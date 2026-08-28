"""Every HTTP route, against the fake device stack."""

import pytest
from fastapi.testclient import TestClient

from sesame import server as server_module
from sesame.server import create_app

TAIPEI = {"lat": 25.0330, "lon": 121.5654}
ROUTE = {"points": [[25.0330, 121.5654], [25.0430, 121.5654]], "speed_kmh": 36.0}


@pytest.fixture
def client(device, monkeypatch):
    async def one_device():
        return [
            {
                "udid": "UDID-1",
                "name": "peter 的 iPhone",
                "iosVersion": "26.6.1",
                "productType": "iPhone16,2",
                "transport": "wifi",
                "online": True,
            }
        ]

    monkeypatch.setattr(server_module, "list_devices", one_device)
    with TestClient(create_app()) as made:
        yield made


def connect(client):
    response = client.post("/api/connect", json={"udid": "UDID-1"})
    assert response.status_code == 200
    return response.json()


def test_devices_lists_what_the_daemon_sees(client):
    body = client.get("/api/devices").json()
    assert body["devices"][0]["name"] == "peter 的 iPhone"


def test_devices_reports_an_unreachable_daemon(client, monkeypatch):
    async def unreachable():
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(server_module, "list_devices", unreachable)
    response = client.get("/api/devices")
    assert response.status_code == 503
    assert "tunneld" in response.json()["detail"]


def test_connect_then_disconnect(client):
    body = connect(client)
    assert body["session"]["state"] == "connected"
    assert body["session"]["deviceName"] == "peter 的 iPhone"

    body = client.post("/api/disconnect").json()
    assert body["session"]["state"] == "disconnected"


def test_connect_to_a_device_that_is_not_there(client, device):
    device.available = False
    response = client.post("/api/connect", json={"udid": "UDID-1"})
    assert response.status_code == 502


def test_location_reaches_the_device(client, device):
    connect(client)
    assert client.post("/api/location", json=TAIPEI).status_code == 200
    client.portal.call(_settle, lambda: device.simulation.writes)
    assert device.simulation.writes[-1] == (TAIPEI["lat"], TAIPEI["lon"])


def test_location_stops_a_running_route(client):
    connect(client)
    client.post("/api/route/start", json=ROUTE)
    assert client.get("/api/status").json()["route"]["running"] is True

    client.post("/api/location", json=TAIPEI)
    assert client.get("/api/status").json()["route"]["running"] is False


def test_clear_hands_gps_back(client, device):
    connect(client)
    client.post("/api/location", json=TAIPEI)
    assert client.post("/api/clear").status_code == 200
    assert device.simulation.cleared == 1


def test_clear_without_a_device_is_harmless(client):
    assert client.post("/api/clear").status_code == 200


def test_route_requires_a_device(client):
    assert client.post("/api/route/start", json=ROUTE).status_code == 409


def test_route_rejects_a_zero_length_path(client):
    connect(client)
    response = client.post(
        "/api/route/start",
        json={"points": [[25.0, 121.0], [25.0, 121.0]], "speed_kmh": 5.0},
    )
    assert response.status_code == 400


def test_route_rejects_a_single_point(client):
    connect(client)
    response = client.post("/api/route/start", json={"points": [[25.0, 121.0]]})
    assert response.status_code == 422


def test_route_lifecycle(client):
    connect(client)
    body = client.post("/api/route/start", json=ROUTE).json()
    assert body["route"]["running"] is True
    assert body["route"]["speedKmh"] == 36.0

    body = client.post("/api/route/pause").json()
    assert body["route"]["paused"] is True

    body = client.post("/api/route/resume").json()
    assert body["route"]["paused"] is False

    body = client.post("/api/route/speed", json={"speed_kmh": 90.0}).json()
    assert body["route"]["speedKmh"] == 90.0

    body = client.post("/api/route/seek", json={"fraction": 0.5}).json()
    assert body["route"]["progress"] == pytest.approx(0.5, abs=0.05)

    body = client.post("/api/route/stop").json()
    assert body["route"]["running"] is False


def test_route_speed_is_bounded(client):
    connect(client)
    client.post("/api/route/start", json=ROUTE)
    assert client.post("/api/route/speed", json={"speed_kmh": 0}).status_code == 422
    assert client.post("/api/route/speed", json={"speed_kmh": -5}).status_code == 422


def test_seek_fraction_is_bounded(client):
    connect(client)
    client.post("/api/route/start", json=ROUTE)
    assert client.post("/api/route/seek", json={"fraction": 1.5}).status_code == 422
    assert client.post("/api/route/seek", json={"fraction": -0.1}).status_code == 422


def test_jitter_is_bounded(client):
    connect(client)
    response = client.post("/api/route/start", json={**ROUTE, "jitter_m": 500})
    assert response.status_code == 422


def test_mount_requires_a_device(client):
    assert client.post("/api/mount").status_code == 409


def test_disconnect_also_stops_the_route(client):
    connect(client)
    client.post("/api/route/start", json=ROUTE)
    body = client.post("/api/disconnect").json()
    assert body["route"]["running"] is False


def test_websocket_receives_updates_as_the_device_moves(client):
    connect(client)
    with client.websocket_connect("/ws") as socket:
        socket.receive_json()  # the snapshot sent on connect
        client.post("/api/location", json=TAIPEI)
        for _ in range(20):
            body = socket.receive_json()
            if body["session"]["lat"] is not None:
                assert body["session"]["lat"] == pytest.approx(TAIPEI["lat"])
                return
    raise AssertionError("no position update arrived")


async def _settle(condition, timeout=2.0):
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def test_a_broken_client_is_dropped_from_the_broadcast():
    import asyncio

    from sesame.server import Broadcaster

    class Client:
        def __init__(self, works):
            self.works = works
            self.sent = 0

        async def send_json(self, payload):
            if not self.works:
                raise ConnectionResetError("gone")
            self.sent += 1

    good, bad = Client(True), Client(False)
    broadcaster = Broadcaster()
    broadcaster.add(good)
    broadcaster.add(bad)

    asyncio.run(broadcaster.send({"x": 1}))
    assert good.sent == 1
    # A client that failed once must not be tried again, or every later
    # broadcast pays for it.
    asyncio.run(broadcaster.send({"x": 2}))
    assert good.sent == 2
    assert broadcaster.empty is False

    broadcaster.remove(good)
    assert broadcaster.empty is True


def test_missing_icon_is_a_404(client, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(server_module, "ASSETS_DIR", Path("/nonexistent"))
    assert client.get("/icon.png").status_code == 404


def test_diagnose_endpoint_returns_the_finding(client, monkeypatch):
    async def finding():
        return {"ok": False, "reason": "卡住了", "actions": ["重啟"], "storedMacs": []}

    monkeypatch.setattr(server_module, "diagnose_wifi", finding)
    body = client.get("/api/diagnose").json()
    assert body["ok"] is False
    assert body["actions"] == ["重啟"]


def test_mount_reports_a_device_that_refuses(client, monkeypatch):
    connect(client)

    async def broken_mount(lockdown):
        raise RuntimeError("image not found")

    async def fake_lockdown(serial=None, **kwargs):
        return object()

    import pymobiledevice3.lockdown as lockdown_module
    import pymobiledevice3.services.mobile_image_mounter as mounter_module

    monkeypatch.setattr(lockdown_module, "create_using_usbmux", fake_lockdown)
    monkeypatch.setattr(mounter_module, "auto_mount", broken_mount)

    response = client.post("/api/mount")
    assert response.status_code == 502
    assert "RuntimeError" in response.json()["detail"]


def test_mount_succeeds_against_a_willing_device(client, monkeypatch):
    connect(client)
    mounted = []

    async def auto_mount(lockdown):
        mounted.append(True)

    async def fake_lockdown(serial=None, **kwargs):
        return object()

    import pymobiledevice3.lockdown as lockdown_module
    import pymobiledevice3.services.mobile_image_mounter as mounter_module

    monkeypatch.setattr(lockdown_module, "create_using_usbmux", fake_lockdown)
    monkeypatch.setattr(mounter_module, "auto_mount", auto_mount)

    assert client.post("/api/mount").json() == {"mounted": True}
    assert mounted == [True]


def test_a_lost_session_stops_the_route(client, device):
    import time

    connect(client)
    client.post("/api/route/start", json=ROUTE)
    assert client.get("/api/status").json()["route"]["running"] is True

    # The device is gone for good and every reconnect attempt will fail.
    device.available = False
    device.fail_opens = 99
    device.simulation.fail_next = ConnectionResetError("peer went away")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        body = client.get("/api/status").json()
        if body["session"]["state"] == "error" and not body["route"]["running"]:
            return
        time.sleep(0.05)

    # A route still reporting progress against a dead session is a lie.
    raise AssertionError(f"route never stopped: {client.get('/api/status').json()}")
