"""HTTP + WebSocket front end for the location engine."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sesame.engine import DeviceSession, RouteRunner, list_devices

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


def create_app() -> FastAPI:
    broadcaster = Broadcaster()

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
            return {"devices": await list_devices()}
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

    def _require_connected() -> None:
        if session.status.udid is None:
            raise HTTPException(status_code=409, detail="connect a device first")

    return app
