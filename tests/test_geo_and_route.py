"""Constant-speed traversal is the behaviour these tests exist to protect."""

import asyncio

import pytest

from sesame import engine
from sesame.engine import RouteRunner
from sesame.geo import Path, bearing, haversine, interpolate, offset

TAIPEI = (25.0330, 121.5654)


class RecordingSession:
    """Stands in for a connected device; records what would have been written."""

    def __init__(self) -> None:
        self.pushes: list[tuple[float, float]] = []

    def push(self, location, heading=0.0) -> None:
        self.pushes.append(location)


def test_path_length_matches_leg_sum():
    a, b, c = TAIPEI, (25.0430, 121.5654), (25.0430, 121.5800)
    path = Path.from_points([a, b, c])
    assert path.length == pytest.approx(haversine(a, b) + haversine(b, c), rel=1e-9)


def test_duplicate_points_are_dropped():
    path = Path.from_points([TAIPEI, TAIPEI, (25.0430, 121.5654)])
    assert len(path.points) == 2


def test_at_clamps_outside_the_path():
    path = Path.from_points([TAIPEI, (25.0430, 121.5654)])
    assert path.at(-100)[0] == pytest.approx(TAIPEI)
    assert path.at(path.length + 500)[0] == pytest.approx(path.points[-1])


def test_at_is_uniform_across_uneven_vertices():
    # A 2 m hop followed by a ~1 km leg: sampling by arc length must not care.
    path = Path.from_points([TAIPEI, (25.03302, 121.5654), (25.0430, 121.5654)])
    step = path.length / 200
    samples = [path.at(i * step)[0] for i in range(201)]
    gaps = [haversine(samples[i], samples[i + 1]) for i in range(len(samples) - 1)]
    assert max(gaps) - min(gaps) < 0.05  # meters


def test_interpolate_midpoint_is_equidistant():
    a, b = TAIPEI, (25.0430, 121.5800)
    middle = interpolate(a, b, 0.5)
    assert haversine(a, middle) == pytest.approx(haversine(middle, b), rel=1e-6)


def test_offset_moves_the_requested_distance():
    moved = offset(TAIPEI, 100.0, 45.0)
    assert haversine(TAIPEI, moved) == pytest.approx(100.0, abs=0.01)
    assert bearing(TAIPEI, moved) == pytest.approx(45.0, abs=0.01)


async def test_route_holds_the_requested_ground_speed():
    session = RecordingSession()
    runner = RouteRunner(session, tick_hz=20)
    await runner.start([TAIPEI, (25.03302, 121.5654), (25.0430, 121.5654)], speed_kmh=36.0)
    await asyncio.sleep(1.0)
    await runner.stop()

    # 36 km/h == 10 m/s; allow one tick of slack at each end.
    assert runner.status.distance_m == pytest.approx(10.0, abs=1.0)
    travelled = haversine(session.pushes[0], session.pushes[-1])
    assert travelled == pytest.approx(10.0, abs=1.0)


async def test_speed_change_applies_without_restart():
    runner = RouteRunner(RecordingSession(), tick_hz=20)
    await runner.start([TAIPEI, (25.0430, 121.5654)], speed_kmh=36.0)
    await asyncio.sleep(0.5)
    runner.set_speed(360.0)
    await asyncio.sleep(0.5)
    await runner.stop()
    # ~5 m at 10 m/s then ~50 m at 100 m/s.
    assert runner.status.distance_m == pytest.approx(55.0, abs=8.0)


async def test_pause_does_not_accumulate_distance():
    runner = RouteRunner(RecordingSession(), tick_hz=20)
    await runner.start([TAIPEI, (25.0430, 121.5654)], speed_kmh=360.0)
    await asyncio.sleep(0.3)
    runner.pause()
    paused_at = runner.status.distance_m
    await asyncio.sleep(0.6)
    assert runner.status.distance_m == paused_at

    runner.resume()
    await asyncio.sleep(0.3)
    await runner.stop()
    # Only the two ~0.3 s running windows count, not the pause between them.
    assert runner.status.distance_m == pytest.approx(60.0, abs=12.0)


async def test_loop_wraps_instead_of_stopping():
    runner = RouteRunner(RecordingSession(), tick_hz=20)
    # ~2.2 m path at 100 m/s wraps many times in well under a second.
    await runner.start([TAIPEI, (25.03302, 121.5654)], speed_kmh=360.0, loop=True)
    await asyncio.sleep(0.5)
    assert runner.status.running
    assert runner.status.distance_m <= runner.status.total_m
    await runner.stop()


async def test_run_ends_at_the_final_vertex():
    session = RecordingSession()
    runner = RouteRunner(session, tick_hz=20)
    end = (25.03302, 121.5654)
    await runner.start([TAIPEI, end], speed_kmh=360.0)
    await asyncio.sleep(0.5)
    assert not runner.status.running
    assert session.pushes[-1] == pytest.approx(end)


# -- device listing --------------------------------------------------------


class FakeRsd:
    """Enough of RemoteServiceDiscoveryService for the naming/transport logic."""

    def __init__(self, udid, name=None, all_values=None, properties=None, product_version="18.0"):
        self.udid = udid
        self.name = name
        self.all_values = all_values or {}
        self.peer_info = {"Properties": properties or {}}
        self.product_version = product_version
        self.closed = False

    async def close(self):
        self.closed = True


def test_device_name_prefers_the_lockdown_name():
    rsd = FakeRsd(
        "UDID-1", all_values={"DeviceName": "Peter 的 iPhone"}, properties={"ProductType": "iPhone16,2"}
    )
    assert engine.device_name(rsd) == "Peter 的 iPhone"


def test_device_name_falls_back_to_model_then_udid():
    # The RSD handshake carries no human name, which is why the UDID used to leak through.
    assert engine.device_name(FakeRsd("UDID-1", properties={"ProductType": "iPhone16,2"})) == "iPhone16,2"
    assert engine.device_name(FakeRsd("UDID-1")) == "UDID-1"


@pytest.mark.parametrize(
    ("interface", "expected"),
    [
        ("usbmux-00008130-001A2B3C-USB", "usb"),
        ("usbmux-00008130-001A2B3C-Network", "wifi"),
        ("mobdev2-00008130-001A2B3C-192.168.1.42", "wifi"),
        ("fdf5:3c8e:1a2b::1", "usb"),
        ("fe80::1c4d:aeff:fe12:3456%en5", "usb"),
        ("Peters-iPhone.local", "wifi"),
        (None, "unknown"),
    ],
)
def test_transport_of(interface, expected):
    assert engine.transport_of(interface) == expected


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "devices.json"
    monkeypatch.setattr(engine, "STORE_PATH", path)
    return path


async def test_list_devices_names_and_labels_transport(store, monkeypatch):
    rsd = FakeRsd(
        "UDID-1",
        name="usbmux-UDID-1-USB",
        all_values={"DeviceName": "測試機"},
        properties={"ProductType": "iPhone16,2"},
    )

    async def fake_tunneld_devices():
        return [rsd]

    monkeypatch.setattr(engine, "get_tunneld_devices", fake_tunneld_devices)
    devices = await engine.list_devices()

    assert devices == [
        {
            "udid": "UDID-1",
            "name": "測試機",
            "iosVersion": "18.0",
            "productType": "iPhone16,2",
            "transport": "usb",
            "online": True,
        }
    ]
    assert rsd.closed


async def test_list_devices_prefers_usb_over_wifi_for_one_device(store, monkeypatch):
    over_wifi = FakeRsd("UDID-1", name="mobdev2-UDID-1-192.168.1.9", all_values={"DeviceName": "測試機"})
    over_usb = FakeRsd("UDID-1", name="usbmux-UDID-1-USB", all_values={"DeviceName": "測試機"})

    async def fake_tunneld_devices():
        return [over_wifi, over_usb]

    monkeypatch.setattr(engine, "get_tunneld_devices", fake_tunneld_devices)
    devices = await engine.list_devices()
    assert len(devices) == 1
    assert devices[0]["transport"] == "usb"


async def test_remembered_devices_survive_going_offline(store, monkeypatch):
    seen = FakeRsd("UDID-1", name="usbmux-UDID-1-USB", all_values={"DeviceName": "測試機"})

    async def with_device():
        return [seen]

    async def without_device():
        return []

    monkeypatch.setattr(engine, "get_tunneld_devices", with_device)
    await engine.list_devices()

    monkeypatch.setattr(engine, "get_tunneld_devices", without_device)
    devices = await engine.list_devices()

    assert len(devices) == 1
    assert devices[0]["name"] == "測試機"
    assert devices[0]["online"] is False
    assert devices[0]["lastSeen"]


async def test_online_device_is_not_duplicated_by_its_stored_copy(store, monkeypatch):
    seen = FakeRsd("UDID-1", name="usbmux-UDID-1-USB", all_values={"DeviceName": "測試機"})

    async def fake_tunneld_devices():
        return [seen]

    monkeypatch.setattr(engine, "get_tunneld_devices", fake_tunneld_devices)
    await engine.list_devices()
    devices = await engine.list_devices()
    assert [device["udid"] for device in devices] == ["UDID-1"]


def test_unreadable_store_is_treated_as_empty(store):
    store.write_text("{ not json")
    assert engine.load_known_devices() == {}
