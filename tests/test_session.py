"""DeviceSession: one connection held open, newest-coordinate-wins, reconnects.

These are the behaviours the whole design rests on, so they are pinned here
against a fake device rather than left to integration testing on hardware.
"""

import asyncio

import pytest

from sesame import engine
from sesame.engine import DeviceSession, State
from tests.conftest import settle


@pytest.fixture
async def session(device):
    made = DeviceSession()
    yield made
    await made.disconnect()


async def test_connect_opens_the_channel_once(session, device):
    await session.connect("UDID-1")

    assert session.status.state is State.CONNECTED
    assert session.status.device_name == "peter 的 iPhone"
    assert session.status.ios_version == "26.6.1"
    assert len(device.simulations) == 1
    assert device.simulation.entered


async def test_many_updates_reuse_the_same_channel(session, device):
    await session.connect("UDID-1")
    for index in range(20):
        session.push((25.0 + index * 0.001, 121.5))
        await asyncio.sleep(0.02)
    assert await settle(lambda: session.status.writes >= 5)

    # The point of the design: no channel is rebuilt per coordinate.
    assert len(device.simulations) == 1
    assert device.rsd.closed == 0


async def test_a_fast_producer_is_coalesced_not_queued(session, device):
    await session.connect("UDID-1")
    # Far more pushes than the write interval can carry.
    for index in range(200):
        session.push((25.0 + index * 0.0001, 121.5))
    assert await settle(lambda: session.status.writes >= 1)
    await asyncio.sleep(0.1)

    # Intermediate points are skipped rather than backing up, and the newest
    # coordinate is the one that lands.
    assert len(device.simulation.writes) < 200
    assert device.simulation.writes[-1] == pytest.approx((25.0 + 199 * 0.0001, 121.5))


async def test_a_failed_write_reconnects_and_replays_the_coordinate(session, device):
    await session.connect("UDID-1")
    device.simulation.fail_next = ConnectionResetError("peer went away")

    session.push((25.5, 121.5))
    assert await settle(lambda: session.status.reconnects == 1)
    assert await settle(lambda: session.status.state is State.CONNECTED)

    # A new channel, and the coordinate that failed is not lost.
    assert len(device.simulations) == 2
    assert await settle(lambda: (25.5, 121.5) in device.simulation.writes)


async def test_reconnect_retries_until_the_tunnel_returns(session, device):
    await session.connect("UDID-1")
    device.fail_opens = 2
    device.simulation.fail_next = ConnectionResetError("peer went away")

    session.push((25.5, 121.5))
    assert await settle(lambda: session.status.reconnects == 1, timeout=3)
    assert session.status.state is State.CONNECTED
    assert session.status.error is None
    # Two failed attempts and the one that worked.
    assert device.opens >= 4


async def test_giving_up_leaves_a_readable_error(session, device):
    await session.connect("UDID-1")
    device.fail_opens = 99  # longer than RECONNECT_BACKOFF
    device.simulation.fail_next = ConnectionResetError("peer went away")

    session.push((25.5, 121.5))
    assert await settle(lambda: session.status.state is State.ERROR, timeout=3)
    assert "unreachable" in session.status.error


async def test_connect_without_a_tunnel_reports_and_raises(session, device):
    device.available = False
    with pytest.raises(RuntimeError, match="no tunnel"):
        await session.connect("UDID-1")
    assert session.status.state is State.ERROR
    assert "no tunnel" in session.status.error


async def test_disconnect_closes_everything_it_opened(session, device):
    await session.connect("UDID-1")
    simulation = device.simulation
    await session.disconnect()

    assert simulation.exited
    assert device.rsd.closed == 1
    assert session.status.state is State.DISCONNECTED
    assert session.status.udid is None


async def test_reconnecting_to_another_device_replaces_the_first(session, device):
    await session.connect("UDID-1")
    first = device.simulation
    await session.connect("UDID-1")

    assert first.exited
    assert len(device.simulations) == 2
    assert device.rsd.closed == 1


async def test_clear_hands_gps_back_and_drops_pending_writes(session, device):
    await session.connect("UDID-1")
    session.push((25.5, 121.5))
    assert await settle(lambda: session.status.writes >= 1)

    session.push((26.0, 122.0))
    await session.clear()

    assert device.simulation.cleared == 1
    assert session.status.location is None
    await asyncio.sleep(0.1)
    # The queued coordinate must not arrive after control was handed back.
    assert (26.0, 122.0) not in device.simulation.writes


async def test_status_reports_position_and_counters(session, device):
    await session.connect("UDID-1")
    session.push((25.5, 121.5), heading=90.0)
    assert await settle(lambda: session.status.writes == 1)

    body = session.status.as_dict()
    assert body["lat"] == 25.5
    assert body["lon"] == 121.5
    assert body["heading"] == 90.0
    assert body["state"] == "connected"
    assert body["deviceName"] == "peter 的 iPhone"


async def test_change_notifications_fire_on_writes():
    calls = []

    async def on_change():
        calls.append(True)

    made = DeviceSession(on_change=on_change)
    assert made.status.state is State.DISCONNECTED
    # No device needed: the callback is what is under test.
    await made._notify()
    assert calls == [True]


async def test_pushing_while_disconnected_is_harmless(session):
    session.push((25.5, 121.5))
    await asyncio.sleep(0.05)
    assert session.status.writes == 0


async def test_a_stale_coordinate_is_not_sent_to_the_next_device(session, device):
    await session.connect("UDID-1")
    await session.disconnect()

    # Pushed with nothing connected, then a fresh connection is made.
    session.push((99.0, 99.0))
    await session.connect("UDID-1")
    await asyncio.sleep(0.1)

    assert (99.0, 99.0) not in device.simulation.writes


async def test_writes_are_throttled_to_the_interval(session, device, monkeypatch):
    monkeypatch.setattr(engine, "MIN_WRITE_INTERVAL", 0.05)
    await session.connect("UDID-1")

    started = asyncio.get_running_loop().time()
    for index in range(5):
        session.push((25.0 + index, 121.5))
        await asyncio.sleep(0.06)
    assert await settle(lambda: session.status.writes >= 4)
    elapsed = asyncio.get_running_loop().time() - started

    # Five throttled writes cannot have happened faster than four intervals.
    assert elapsed >= 0.2


async def test_writing_without_a_channel_is_refused(session):
    with pytest.raises(RuntimeError, match="not connected"):
        await session._write((25.0, 121.0))


async def test_disconnecting_mid_write_does_not_reconnect(session, device):
    await session.connect("UDID-1")
    device.simulation.write_delay = 0.3
    session.push((25.5, 121.5))
    await asyncio.sleep(0.05)

    # Tearing down while a write is in flight must cancel it, not treat the
    # cancellation as a dropped link and start reconnecting.
    await session.disconnect()
    await asyncio.sleep(0.1)

    assert session.status.state is State.DISCONNECTED
    assert session.status.reconnects == 0
    assert len(device.simulations) == 1


async def test_a_failure_during_shutdown_does_not_reconnect(session, device):
    await session.connect("UDID-1")
    session._closing = True
    device.simulation.fail_next = ConnectionResetError("peer went away")

    session.push((25.5, 121.5))
    await asyncio.sleep(0.15)

    assert session.status.reconnects == 0
    assert len(device.simulations) == 1


async def test_a_channel_that_fails_to_close_is_not_fatal(session, device, monkeypatch):
    await session.connect("UDID-1")

    async def refuse_to_close(*_):
        raise OSError("socket already gone")

    monkeypatch.setattr(type(device.simulation), "__aexit__", refuse_to_close)
    await session.disconnect()

    # A device yanked mid-session makes close() throw; the session still ends.
    assert session.status.state is State.DISCONNECTED


async def test_a_broken_listener_does_not_kill_the_writer(device):
    calls = []

    async def broken():
        calls.append(True)
        raise RuntimeError("listener blew up")

    made = DeviceSession(on_change=broken)
    try:
        await made.connect("UDID-1")
        made.push((25.5, 121.5))
        assert await settle(lambda: made.status.writes >= 1)

        # Still writing after the listener threw on an earlier update.
        made.push((26.0, 122.0))
        assert await settle(lambda: (26.0, 122.0) in device.simulation.writes)
        assert len(calls) > 1
    finally:
        await made.disconnect()
