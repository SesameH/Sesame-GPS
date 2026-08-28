"""A fake device stack, so the session logic can be tested without hardware."""

import asyncio

import pytest

from sesame import engine


class FakeSimulation:
    """Stands in for LocationSimulation, recording what reached the device."""

    def __init__(self, provider):
        self.provider = provider
        self.writes: list[tuple[float, float]] = []
        self.cleared = 0
        self.entered = False
        self.exited = False
        # Set to raise on the next set(); the writer should treat it as a
        # dropped link rather than losing the coordinate.
        self.fail_next: Exception | None = None
        self.write_delay = 0.0

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_):
        self.exited = True

    async def set(self, latitude, longitude):
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.writes.append((latitude, longitude))

    async def clear(self):
        self.cleared += 1


class FakeProvider:
    def __init__(self, rsd):
        self.rsd = rsd
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.exited = True


class FakeRsd:
    def __init__(self, udid="UDID-1", name="peter 的 iPhone", version="26.6.1"):
        self.udid = udid
        self.name = "usbmux-UDID-1-USB"
        self.all_values = {"DeviceName": name}
        self.peer_info = {"Properties": {"ProductType": "iPhone16,2"}}
        self.product_version = version
        self.closed = 0

    async def close(self):
        self.closed += 1


@pytest.fixture
def device(monkeypatch):
    """Wire the fake stack into the engine and hand back a handle to it."""

    class Device:
        def __init__(self):
            self.rsd = FakeRsd()
            self.simulations: list[FakeSimulation] = []
            self.opens = 0
            # Number of upcoming open attempts that should fail, for reconnects.
            self.fail_opens = 0
            self.available = True

        @property
        def simulation(self):
            return self.simulations[-1]

    handle = Device()

    async def fake_lookup(udid):
        handle.opens += 1
        if handle.fail_opens > 0:
            handle.fail_opens -= 1
            raise ConnectionError("tunnel not ready")
        return handle.rsd if handle.available else None

    def fake_simulation(provider):
        simulation = FakeSimulation(provider)
        handle.simulations.append(simulation)
        return simulation

    monkeypatch.setattr(engine, "get_tunneld_device_by_udid", fake_lookup)
    monkeypatch.setattr(engine, "DvtProvider", FakeProvider)
    monkeypatch.setattr(engine, "LocationSimulation", fake_simulation)
    # Keep reconnect backoff from dominating the test run.
    monkeypatch.setattr(engine, "RECONNECT_BACKOFF", (0.01, 0.01, 0.01))
    monkeypatch.setattr(engine, "MIN_WRITE_INTERVAL", 0.01)
    return handle


async def settle(condition, timeout=2.0, interval=0.01):
    """Wait for a condition the writer task reaches on its own schedule."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False
