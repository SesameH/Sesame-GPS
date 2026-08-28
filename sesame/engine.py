"""Persistent location-simulation session and constant-speed route playback.

Two ideas carry this module:

* One DTX channel for the whole session.  ``LocationSimulation`` is opened once
  and every update is a plain ``set()`` on that live channel.  Nothing is torn
  down or rebuilt per coordinate, so rapid updates cost one RPC each instead of
  a fresh RSD + DVT handshake.
* A single-slot coalescing mailbox in front of it.  Producers overwrite the
  pending coordinate rather than queueing, so a fast producer can never
  outrun the device -- it just skips intermediate points.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path as FilePath

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid, get_tunneld_devices

from sesame.geo import LatLon, Path, offset

logger = logging.getLogger(__name__)

# How long the writer waits between two consecutive DTX writes.  iOS accepts
# updates far faster than this, but there is nothing to gain from it: CoreLocation
# coalesces anyway, and a tighter loop only makes the channel more likely to stall.
MIN_WRITE_INTERVAL = 0.1

RECONNECT_BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0)


class State(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class SessionStatus:
    state: State = State.DISCONNECTED
    udid: str | None = None
    device_name: str | None = None
    ios_version: str | None = None
    location: LatLon | None = None
    heading: float = 0.0
    error: str | None = None
    writes: int = 0
    reconnects: int = 0

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "udid": self.udid,
            "deviceName": self.device_name,
            "iosVersion": self.ios_version,
            "lat": None if self.location is None else self.location[0],
            "lon": None if self.location is None else self.location[1],
            "heading": self.heading,
            "error": self.error,
            "writes": self.writes,
            "reconnects": self.reconnects,
        }


def device_properties(rsd: RemoteServiceDiscoveryService) -> dict:
    """``peer_info["Properties"]`` from the RSD handshake, or an empty dict."""
    if rsd.peer_info is None:
        return {}
    return rsd.peer_info.get("Properties", {})


def device_name(rsd: RemoteServiceDiscoveryService) -> str:
    """The name the user gave the device, e.g. "Peter's iPhone".

    The RSD handshake does *not* carry it -- ``peer_info["Properties"]`` holds
    only identifiers (``UniqueDeviceID``, ``ProductType``, ``OSVersion``, ...).
    The human name comes from the remote lockdown client that ``connect()``
    opens, so it lives in ``all_values``.  An untrusted lockdown may expose
    nothing, hence the walk down to the model and finally the UDID.
    """
    properties = device_properties(rsd)
    return (
        rsd.all_values.get("DeviceName")
        or properties.get("Name")
        or properties.get("ProductType")
        or rsd.udid
    )


def transport_of(interface: str | None) -> str:
    """Classify a tunnel by the interface name tunneld reports for it.

    tunneld keys each tunnel by how it found the device, and that key comes
    back as the ``interface`` field:

    ==========================  =========================================
    ``usbmux-<serial>-USB``     device on the cable, via usbmuxd
    ``usbmux-<serial>-Network`` device over Wi-Fi, via usbmuxd
    ``mobdev2-<udid>-<ip>``     paired device found over Wi-Fi (Bonjour)
    ``fdxx::…`` / ``fe80::…``   USB CDC-NCM interface
    ``<hostname>``              RemotePairing service over Wi-Fi
    ==========================  =========================================
    """
    if not interface:
        return "unknown"
    if interface.startswith("usbmux-"):
        return "usb" if interface.rsplit("-", 1)[-1].lower() == "usb" else "wifi"
    if interface.startswith("mobdev2-"):
        return "wifi"
    if ":" in interface:
        return "usb"
    return "wifi"


async def list_devices() -> list[dict]:
    """Devices tunneld currently has a tunnel for, plus ones seen before.

    Devices that are not reachable right now are still listed, marked
    ``online: False``, so a phone that has been connected once stays
    recognisable by name instead of vanishing from the picker.
    """
    rsds = await get_tunneld_devices()
    devices = []
    for rsd in rsds:
        try:
            devices.append(
                {
                    "udid": rsd.udid,
                    "name": device_name(rsd),
                    "iosVersion": rsd.product_version,
                    "productType": device_properties(rsd).get("ProductType"),
                    "transport": transport_of(rsd.name),
                    "online": True,
                }
            )
        finally:
            await rsd.close()

    # tunneld can advertise the same device on several interfaces; prefer the
    # cable, which survives the phone sleeping or roaming between APs.
    unique: dict[str, dict] = {}
    for device in devices:
        existing = unique.get(device["udid"])
        if existing is None or (existing["transport"] != "usb" and device["transport"] == "usb"):
            unique[device["udid"]] = device

    for device in unique.values():
        remember_device(device)

    known = load_known_devices()
    offline = [{**entry, "online": False} for udid, entry in known.items() if udid not in unique]
    offline.sort(key=lambda entry: entry.get("lastSeen", ""), reverse=True)
    return list(unique.values()) + offline


# -- remembered devices ----------------------------------------------------

STORE_PATH = FilePath.home() / ".sesame" / "devices.json"


def load_known_devices() -> dict[str, dict]:
    """Devices seen in earlier sessions, keyed by UDID."""
    try:
        with STORE_PATH.open() as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_device(device: dict) -> None:
    """Record a device so it stays in the picker once it goes away."""
    known = load_known_devices()
    known[device["udid"]] = {
        "udid": device["udid"],
        "name": device["name"],
        "iosVersion": device.get("iosVersion"),
        "productType": device.get("productType"),
        "transport": device.get("transport"),
        "lastSeen": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with STORE_PATH.open("w") as handle:
            json.dump(known, handle, indent=2)
    except OSError as error:
        logger.debug("could not persist known devices: %r", error)


class DeviceSession:
    """Owns the long-lived tunnel + DVT + LocationSimulation triple for one device."""

    def __init__(self, on_change: Callable[[], Awaitable[None]] | None = None) -> None:
        self.status = SessionStatus()
        self._on_change = on_change
        self._rsd: RemoteServiceDiscoveryService | None = None
        self._provider: DvtProvider | None = None
        self._simulation: LocationSimulation | None = None
        self._writer: asyncio.Task | None = None
        # Single-slot mailbox: newest coordinate wins, older pending ones are dropped.
        self._pending: LatLon | None = None
        self._pending_heading: float = 0.0
        self._wakeup = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closing = False

    # -- lifecycle ---------------------------------------------------------

    async def connect(self, udid: str) -> None:
        async with self._lock:
            await self._teardown()
            self._closing = False
            self.status = SessionStatus(state=State.CONNECTING, udid=udid)
            await self._notify()
            try:
                await self._open(udid)
            except Exception as error:
                self.status.state = State.ERROR
                self.status.error = f"{type(error).__name__}: {error}"
                await self._notify()
                raise
            self.status.state = State.CONNECTED
            self.status.error = None
            self._writer = asyncio.create_task(self._writer_loop(), name="sesame-writer")
            await self._notify()

    async def disconnect(self) -> None:
        async with self._lock:
            self._closing = True
            await self._teardown()
            self.status = SessionStatus(state=State.DISCONNECTED)
            await self._notify()

    async def _open(self, udid: str) -> None:
        rsd = await get_tunneld_device_by_udid(udid)
        if rsd is None:
            raise RuntimeError(f"no tunnel for {udid}. Start one with: sudo pymobiledevice3 remote tunneld")
        self._rsd = rsd
        self.status.device_name = device_name(rsd)
        self.status.ios_version = rsd.product_version

        provider = DvtProvider(rsd)
        await provider.__aenter__()
        self._provider = provider

        simulation = LocationSimulation(provider)
        await simulation.__aenter__()
        self._simulation = simulation

    async def _teardown(self) -> None:
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
            self._writer = None
        await self._close_transport()

    async def _close_transport(self) -> None:
        for resource in (self._simulation, self._provider, self._rsd):
            if resource is None:
                continue
            with contextlib.suppress(Exception):
                if resource is self._rsd:
                    await resource.close()
                else:
                    await resource.__aexit__(None, None, None)
        self._simulation = self._provider = self._rsd = None

    # -- writing -----------------------------------------------------------

    def push(self, location: LatLon, heading: float = 0.0) -> None:
        """Queue a coordinate, replacing any coordinate not yet written."""
        self._pending = location
        self._pending_heading = heading
        self._wakeup.set()

    async def clear(self) -> None:
        """Hand GPS control back to the device."""
        self._pending = None
        if self._simulation is not None:
            await self._simulation.clear()
        self.status.location = None
        await self._notify()

    async def _writer_loop(self) -> None:
        last_write = 0.0
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()

            elapsed = time.monotonic() - last_write
            if elapsed < MIN_WRITE_INTERVAL:
                await asyncio.sleep(MIN_WRITE_INTERVAL - elapsed)

            location = self._pending
            if location is None:
                continue
            # Take the freshest value available at the moment we actually write.
            self._pending = None
            heading = self._pending_heading

            try:
                await self._write(location)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("location write failed: %r", error)
                # Keep the coordinate so it is replayed once the link is back.
                self._pending = location
                if not await self._reconnect():
                    return
                continue

            last_write = time.monotonic()
            self.status.location = location
            self.status.heading = heading
            self.status.writes += 1
            await self._notify()

    async def _write(self, location: LatLon) -> None:
        if self._simulation is None:
            raise RuntimeError("not connected")
        await self._simulation.set(location[0], location[1])

    async def _reconnect(self) -> bool:
        """Rebuild the DVT channel in place, keeping the session alive.

        Returns ``False`` only when the session is being shut down, in which
        case the writer loop exits.
        """
        udid = self.status.udid
        if udid is None or self._closing:
            return False

        self.status.state = State.RECONNECTING
        await self._notify()

        for attempt, delay in enumerate(RECONNECT_BACKOFF, start=1):
            await self._close_transport()
            await asyncio.sleep(delay)
            if self._closing:
                return False
            try:
                await self._open(udid)
            except Exception as error:
                logger.warning("reconnect attempt %d failed: %r", attempt, error)
                self.status.error = f"{type(error).__name__}: {error}"
                await self._notify()
                continue
            self.status.state = State.CONNECTED
            self.status.error = None
            self.status.reconnects += 1
            self._wakeup.set()
            await self._notify()
            return True

        self.status.state = State.ERROR
        self.status.error = "device unreachable after repeated retries"
        await self._notify()
        return False

    async def _notify(self) -> None:
        if self._on_change is not None:
            await self._on_change()


@dataclass
class RouteStatus:
    running: bool = False
    paused: bool = False
    speed_kmh: float = 5.0
    distance_m: float = 0.0
    total_m: float = 0.0
    loop: bool = False
    jitter_m: float = 0.0

    def as_dict(self) -> dict:
        progress = 0.0 if self.total_m <= 0 else min(1.0, self.distance_m / self.total_m)
        return {
            "running": self.running,
            "paused": self.paused,
            "speedKmh": round(self.speed_kmh, 2),
            "distanceM": round(self.distance_m, 1),
            "totalM": round(self.total_m, 1),
            "progress": progress,
            "loop": self.loop,
            "jitterM": self.jitter_m,
            "etaSeconds": self._eta(),
        }

    def _eta(self) -> float | None:
        speed_mps = self.speed_kmh / 3.6
        if not self.running or speed_mps <= 0 or self.loop:
            return None
        return max(0.0, (self.total_m - self.distance_m) / speed_mps)


class RouteRunner:
    """Walks a :class:`Path` at a constant ground speed.

    Position is integrated as ``distance += speed * dt`` on every tick and then
    resolved against the path's arc length, so the speed the device reports is
    the speed that was asked for regardless of how the polyline was drawn --
    two vertices or two thousand.  Changing the speed mid-run takes effect on
    the next tick without restarting anything.
    """

    def __init__(self, session: DeviceSession, tick_hz: float = 10.0) -> None:
        self._session = session
        self._interval = 1.0 / tick_hz
        self._path: Path | None = None
        self._task: asyncio.Task | None = None
        self._resume = asyncio.Event()
        self._resume.set()
        self.status = RouteStatus()

    async def start(
        self,
        points: list[LatLon],
        speed_kmh: float,
        loop: bool = False,
        jitter_m: float = 0.0,
    ) -> None:
        await self.stop()
        path = Path.from_points(points)
        if path.length <= 0:
            raise ValueError("route has zero length")
        self._path = path
        self.status = RouteStatus(
            running=True,
            paused=False,
            speed_kmh=max(0.0, speed_kmh),
            distance_m=0.0,
            total_m=path.length,
            loop=loop,
            jitter_m=max(0.0, jitter_m),
        )
        self._resume.set()
        self._task = asyncio.create_task(self._run(), name="sesame-route")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.status.running = False
        self.status.paused = False

    def pause(self) -> None:
        if self.status.running:
            self.status.paused = True
            self._resume.clear()

    def resume(self) -> None:
        if self.status.running:
            self.status.paused = False
            self._resume.set()

    def set_speed(self, speed_kmh: float) -> None:
        self.status.speed_kmh = max(0.0, speed_kmh)

    def seek(self, fraction: float) -> None:
        if self._path is not None:
            self.status.distance_m = max(0.0, min(1.0, fraction)) * self._path.length

    async def _run(self) -> None:
        assert self._path is not None
        path = self._path
        previous = time.monotonic()

        # Emit the starting point immediately so the device jumps to the route
        # head instead of waiting out the first tick.
        location, heading = path.at(0.0)
        self._session.push(self._jittered(location), heading)

        while True:
            await asyncio.sleep(self._interval)

            if not self._resume.is_set():
                await self._resume.wait()
                # Time spent paused must not count as travelled distance, so the
                # clock restarts rather than resuming from the pre-pause stamp.
                previous = time.monotonic()
                continue

            now = time.monotonic()
            dt = now - previous
            previous = now

            self.status.distance_m += (self.status.speed_kmh / 3.6) * dt

            if self.status.distance_m >= path.length:
                if self.status.loop:
                    self.status.distance_m = math.fmod(self.status.distance_m, path.length)
                else:
                    self.status.distance_m = path.length
                    location, heading = path.at(path.length)
                    self._session.push(location, heading)
                    self.status.running = False
                    return

            location, heading = path.at(self.status.distance_m)
            self._session.push(self._jittered(location), heading)

    def _jittered(self, location: LatLon) -> LatLon:
        if self.status.jitter_m <= 0:
            return location
        return offset(
            location,
            random.uniform(0.0, self.status.jitter_m),
            random.uniform(0.0, 360.0),
        )
