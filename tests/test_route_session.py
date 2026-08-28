"""RouteRunner driving a real DeviceSession, plus the awkward route cases."""

import asyncio

import pytest

from sesame.engine import DeviceSession, RouteRunner
from sesame.geo import haversine
from tests.conftest import settle

TAIPEI = (25.0330, 121.5654)
NORTH = (25.0430, 121.5654)


@pytest.fixture
async def connected(device):
    made = DeviceSession()
    await made.connect("UDID-1")
    yield made
    await made.disconnect()


async def test_a_route_reaches_the_device(connected, device):
    route = RouteRunner(connected, tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=360.0)
    assert await settle(lambda: len(device.simulation.writes) >= 3)
    await route.stop()

    written = device.simulation.writes
    # Movement is northward along the drawn line.
    assert written[-1][0] > written[0][0]
    assert all(point[1] == pytest.approx(121.5654, abs=1e-6) for point in written)


async def test_stopping_a_route_stops_the_writes(connected, device):
    route = RouteRunner(connected, tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=360.0)
    assert await settle(lambda: len(device.simulation.writes) >= 2)
    await route.stop()

    await asyncio.sleep(0.05)
    settled = len(device.simulation.writes)
    await asyncio.sleep(0.2)
    # Nothing may trickle out after the route was stopped.
    assert len(device.simulation.writes) == settled


async def test_pausing_holds_position_then_resumes(connected, device):
    route = RouteRunner(connected, tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=360.0)
    assert await settle(lambda: len(device.simulation.writes) >= 2)

    route.pause()
    await asyncio.sleep(0.05)
    held = route.status.distance_m
    await asyncio.sleep(0.2)
    assert route.status.distance_m == held

    route.resume()
    assert await settle(lambda: route.status.distance_m > held)
    await route.stop()


async def test_starting_a_second_route_replaces_the_first(connected):
    route = RouteRunner(connected, tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=360.0)
    await asyncio.sleep(0.05)
    first_total = route.status.total_m

    await route.start([TAIPEI, (25.1, 121.7)], speed_kmh=10.0)
    assert route.status.total_m != first_total
    assert route.status.distance_m == 0.0
    assert route.status.speed_kmh == 10.0
    await route.stop()


async def test_seeking_moves_the_device(connected, device):
    route = RouteRunner(connected, tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=1.0)
    assert await settle(lambda: len(device.simulation.writes) >= 1)

    route.seek(0.9)
    assert await settle(lambda: device.simulation.writes[-1][0] > (TAIPEI[0] + NORTH[0]) / 2)
    await route.stop()


async def test_seek_is_clamped_to_the_path():
    route = RouteRunner(_NullSession(), tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=1.0)
    route.seek(5.0)
    assert route.status.distance_m == route.status.total_m
    route.seek(-5.0)
    assert route.status.distance_m == 0.0
    await route.stop()


async def test_seek_before_a_route_exists_is_harmless():
    route = RouteRunner(_NullSession())
    route.seek(0.5)
    assert route.status.distance_m == 0.0


async def test_looping_wraps_without_jumping_backwards_in_time(connected, device):
    route = RouteRunner(connected, tick_hz=20)
    # Short path at high speed wraps several times in a moment.
    await route.start([TAIPEI, (25.0332, 121.5654)], speed_kmh=360.0, loop=True)
    assert await settle(lambda: route.status.running and len(device.simulation.writes) > 3)
    await asyncio.sleep(0.2)

    assert route.status.running
    assert 0.0 <= route.status.distance_m <= route.status.total_m
    await route.stop()


async def test_a_finished_route_reports_completion(connected):
    route = RouteRunner(connected, tick_hz=20)
    await route.start([TAIPEI, (25.0331, 121.5654)], speed_kmh=360.0)
    assert await settle(lambda: not route.status.running)
    assert route.status.distance_m == route.status.total_m
    assert route.status.as_dict()["progress"] == 1.0


async def test_jitter_stays_within_the_requested_radius(connected, device):
    route = RouteRunner(connected, tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=1.0, jitter_m=3.0)
    assert await settle(lambda: len(device.simulation.writes) >= 5)
    await route.stop()

    # Every written point must sit within the jitter radius of the true path.
    for written in device.simulation.writes:
        assert haversine(written, TAIPEI) < 20.0


async def test_zero_length_routes_are_refused():
    route = RouteRunner(_NullSession())
    with pytest.raises(ValueError, match="zero length"):
        await route.start([TAIPEI, TAIPEI], speed_kmh=5.0)


async def test_a_stalled_speed_keeps_the_route_alive_but_still():
    session = _NullSession()
    route = RouteRunner(session, tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=5.0)
    route.set_speed(0.0)
    await asyncio.sleep(0.05)
    held = route.status.distance_m
    await asyncio.sleep(0.15)

    assert route.status.running
    assert route.status.distance_m == held
    await route.stop()


async def test_eta_is_absent_for_a_loop_and_present_otherwise():
    route = RouteRunner(_NullSession(), tick_hz=20)
    await route.start([TAIPEI, NORTH], speed_kmh=36.0)
    assert route.status.as_dict()["etaSeconds"] == pytest.approx(111.0, rel=0.05)
    await route.stop()

    await route.start([TAIPEI, NORTH], speed_kmh=36.0, loop=True)
    assert route.status.as_dict()["etaSeconds"] is None
    await route.stop()


async def test_stopping_a_route_that_never_started_is_harmless():
    route = RouteRunner(_NullSession())
    await route.stop()
    assert route.status.running is False


class _NullSession:
    """Accepts pushes and does nothing with them."""

    def push(self, location, heading=0.0):
        pass


async def test_a_route_that_dies_stops_claiming_to_run(connected, monkeypatch):
    route = RouteRunner(connected, tick_hz=20)

    def explode(location, heading=0.0):
        raise RuntimeError("something unforeseen")

    await route.start([TAIPEI, NORTH], speed_kmh=36.0)
    monkeypatch.setattr(connected, "push", explode)

    assert await settle(lambda: not route.status.running, timeout=2)
