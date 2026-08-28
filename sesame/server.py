"""HTTP + WebSocket front end for the location engine."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path as FilePath

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sesame.engine import (
    DeviceSession,
    PairingError,
    RouteRunner,
    has_pair_record,
    list_devices,
    pair_over_usb,
)

logger = logging.getLogger(__name__)

STATIC_DIR = FilePath(__file__).parent / "static"
ASSETS_DIR = FilePath(__file__).parent / "assets"


class Broadcaster:
    """Fans status snapshots out to every connected browser."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    def add(self, websocket: WebSocket) -> None:
        self._clients.add(websocket)

    def remove(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    @property
    def empty(self) -> bool:
        return not self._clients

    async def send(self, payload: dict) -> None:
        dead: list[WebSocket] = []
        for client in list(self._clients):
            try:
                await client.send_json(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)


class LocationBody(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class ConnectBody(BaseModel):
    udid: str


class RouteBody(BaseModel):
    points: list[tuple[float, float]] = Field(min_length=2)
    speed_kmh: float = Field(default=5.0, gt=0, le=1000)
    loop: bool = False
    jitter_m: float = Field(default=0.0, ge=0, le=50)


class SpeedBody(BaseModel):
    speed_kmh: float = Field(gt=0, le=1000)


class SeekBody(BaseModel):
    fraction: float = Field(ge=0, le=1)


# A browser reload drops the socket and reconnects a moment later, so idleness
# has to be given time to prove itself before it counts as "the tab is closed".
IDLE_GRACE_SECONDS = 20.0


def create_app(on_idle: Callable[[], None] | None = None) -> FastAPI:
    """Build the application.

    :param on_idle: called once the last browser has been gone for
        :data:`IDLE_GRACE_SECONDS`. Used to shut the server down when the app
        was launched for a window the user has since closed.
    """
    broadcaster = Broadcaster()
    idle_timer: asyncio.Task | None = None

    async def publish() -> None:
        await broadcaster.send(snapshot())

    session = DeviceSession(on_change=publish)
    route = RouteRunner(session)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await route.stop()
        await session.disconnect()

    app = FastAPI(title="sesame", lifespan=lifespan)

    def snapshot() -> dict:
        return {"session": session.status.as_dict(), "route": route.status.as_dict()}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/icon.png")
    async def icon() -> FileResponse:
        """The same artwork the ``.app`` bundle uses, for the browser tab."""
        path = ASSETS_DIR / "icon.png"
        if not path.exists():
            raise HTTPException(status_code=404, detail="no icon installed")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/devices")
    async def devices() -> dict:
        try:
            # canDiscoverOverWifi tells the interface whether an empty result is
            # worth waiting on or is simply impossible as configured.
            return {
                "devices": await list_devices(),
                "canDiscoverOverWifi": has_pair_record(),
            }
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"cannot reach tunneld ({type(error).__name__}). "
                    "Start it with: sudo pymobiledevice3 remote tunneld"
                ),
            ) from error

    @app.get("/api/status")
    async def status() -> dict:
        return snapshot()

    @app.post("/api/connect")
    async def connect(body: ConnectBody) -> dict:
        try:
            await session.connect(body.udid)
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return snapshot()

    @app.post("/api/disconnect")
    async def disconnect() -> dict:
        await route.stop()
        await session.disconnect()
        await publish()
        return snapshot()

    @app.post("/api/pair")
    async def pair() -> dict:
        """Pair over USB so the device can be found over Wi-Fi later."""
        try:
            return {"paired": await pair_over_usb()}
        except PairingError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=502, detail=f"{type(error).__name__}: {error}") from error

    @app.post("/api/mount")
    async def mount() -> dict:
        """Mount the Developer Disk Image, needed once per boot on iOS 17+."""
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.mobile_image_mounter import auto_mount

        udid = session.status.udid
        if udid is None:
            raise HTTPException(status_code=409, detail="connect a device first")
        try:
            lockdown = await create_using_usbmux(serial=udid)
            await auto_mount(lockdown)
        except Exception as error:
            raise HTTPException(status_code=502, detail=f"{type(error).__name__}: {error}") from error
        return {"mounted": True}

    @app.post("/api/location")
    async def set_location(body: LocationBody) -> dict:
        _require_connected()
        await route.stop()
        session.push((body.lat, body.lon))
        return snapshot()

    @app.post("/api/clear")
    async def clear() -> dict:
        await route.stop()
        with contextlib.suppress(Exception):
            await session.clear()
        return snapshot()

    @app.post("/api/route/start")
    async def route_start(body: RouteBody) -> dict:
        _require_connected()
        try:
            await route.start(
                [(lat, lon) for lat, lon in body.points],
                speed_kmh=body.speed_kmh,
                loop=body.loop,
                jitter_m=body.jitter_m,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        await publish()
        return snapshot()

    @app.post("/api/route/stop")
    async def route_stop() -> dict:
        await route.stop()
        await publish()
        return snapshot()

    @app.post("/api/route/pause")
    async def route_pause() -> dict:
        route.pause()
        await publish()
        return snapshot()

    @app.post("/api/route/resume")
    async def route_resume() -> dict:
        route.resume()
        await publish()
        return snapshot()

    @app.post("/api/route/speed")
    async def route_speed(body: SpeedBody) -> dict:
        route.set_speed(body.speed_kmh)
        await publish()
        return snapshot()

    @app.post("/api/route/seek")
    async def route_seek(body: SeekBody) -> dict:
        route.seek(body.fraction)
        await publish()
        return snapshot()

    @app.websocket("/ws")
    async def websocket(connection: WebSocket) -> None:
        await connection.accept()
        nonlocal idle_timer
        if idle_timer is not None:
            idle_timer.cancel()
            idle_timer = None
        broadcaster.add(connection)
        try:
            await connection.send_json(snapshot())
            while True:
                # The client never sends anything; this just parks until it leaves.
                await connection.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as error:
            logger.debug("websocket closed: %r", error)
        finally:
            broadcaster.remove(connection)
            start_idle_countdown()

    def start_idle_countdown() -> None:
        nonlocal idle_timer
        if on_idle is None or not broadcaster.empty:
            return
        if idle_timer is not None:
            idle_timer.cancel()
        idle_timer = asyncio.create_task(_idle_countdown(), name="sesame-idle")

    async def _idle_countdown() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(IDLE_GRACE_SECONDS)
            if broadcaster.empty:
                logger.info("no browser for %.0fs, shutting down", IDLE_GRACE_SECONDS)
                on_idle()

    def _require_connected() -> None:
        if session.status.udid is None:
            raise HTTPException(status_code=409, detail="connect a device first")

    return app
