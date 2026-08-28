"""Server behaviour that is not about geometry: idle shutdown and static routes."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from sesame import server as server_module
from sesame.server import create_app


@pytest.fixture
def quick_grace(monkeypatch):
    """Shorten the idle grace so tests do not wait out the real one."""
    monkeypatch.setattr(server_module, "IDLE_GRACE_SECONDS", 0.1)


def test_status_is_served_without_a_device():
    with TestClient(create_app()) as client:
        body = client.get("/api/status").json()
    assert body["session"]["state"] == "disconnected"
    assert body["route"]["running"] is False


def test_icon_is_served_from_inside_the_package():
    with TestClient(create_app()) as client:
        response = client.get("/icon.png")
    # Shipped in the wheel, so this must work for an installed copy too.
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_location_requires_a_device():
    with TestClient(create_app()) as client:
        response = client.post("/api/location", json={"lat": 25.03, "lon": 121.56})
    assert response.status_code == 409


def test_location_rejects_impossible_coordinates():
    with TestClient(create_app()) as client:
        response = client.post("/api/location", json={"lat": 999, "lon": 121.56})
    assert response.status_code == 422


def test_idle_shutdown_fires_after_the_last_client_leaves(quick_grace):
    fired = asyncio.Event()

    app = create_app(on_idle=fired.set)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
        # The socket has closed; the countdown starts here.
        client.portal.call(asyncio.wait_for, fired.wait(), 5)

    assert fired.is_set()


def test_a_reconnecting_browser_cancels_the_shutdown(quick_grace, monkeypatch):
    # A reload drops and remakes the socket; that must not count as leaving.
    monkeypatch.setattr(server_module, "IDLE_GRACE_SECONDS", 1.0)
    calls = []

    app = create_app(on_idle=lambda: calls.append(True))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            client.portal.call(asyncio.sleep, 1.5)
            assert calls == []


def test_without_the_hook_nothing_shuts_down(quick_grace):
    # The default CLI run has no on_idle, so closing a tab must be harmless.
    with TestClient(create_app()) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
        assert client.get("/api/status").status_code == 200


def test_devices_reports_whether_wifi_discovery_is_possible(monkeypatch):
    from sesame import engine

    async def no_devices():
        return []

    monkeypatch.setattr(engine, "get_tunneld_devices", no_devices)
    monkeypatch.setattr(engine, "STORE_PATH", engine.FilePath("/nonexistent/devices.json"))

    monkeypatch.setattr(server_module, "has_pair_record", lambda: False)
    with TestClient(create_app()) as client:
        assert client.get("/api/devices").json()["canDiscoverOverWifi"] is False

    monkeypatch.setattr(server_module, "has_pair_record", lambda: True)
    with TestClient(create_app()) as client:
        assert client.get("/api/devices").json()["canDiscoverOverWifi"] is True


def test_pair_endpoint_reports_a_missing_cable(monkeypatch):
    from sesame import engine

    async def no_cable():
        raise engine.PairingError("沒有偵測到用 USB 連著的裝置。")

    monkeypatch.setattr(server_module, "pair_over_usb", no_cable)
    with TestClient(create_app()) as client:
        response = client.post("/api/pair")
    # A user-fixable situation, not a server fault.
    assert response.status_code == 409
    assert "USB" in response.json()["detail"]


def test_pair_endpoint_returns_the_written_records(monkeypatch):
    async def paired():
        return [{"udid": "UDID-1", "wifiMac": "0e:98:d2:bb:d3:15", "record": "/tmp/x.plist"}]

    monkeypatch.setattr(server_module, "pair_over_usb", paired)
    with TestClient(create_app()) as client:
        body = client.post("/api/pair").json()
    assert body["paired"][0]["wifiMac"] == "0e:98:d2:bb:d3:15"


def test_pair_endpoint_surfaces_an_unexpected_failure(monkeypatch):
    async def broken():
        raise ValueError("device went away")

    monkeypatch.setattr(server_module, "pair_over_usb", broken)
    with TestClient(create_app()) as client:
        response = client.post("/api/pair")
    assert response.status_code == 502
    assert "ValueError" in response.json()["detail"]


def test_index_is_always_revalidated():
    with TestClient(create_app()) as client:
        response = client.get("/")
    # An upgraded interface must not sit behind a heuristically cached copy.
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]


def test_shutdown_hands_gps_back_to_the_device(device):
    """Closing the app must not leave the device stuck on a fake location."""
    app = create_app()
    with TestClient(app) as client:
        client.post("/api/connect", json={"udid": "UDID-1"})
        client.post("/api/location", json={"lat": 25.03, "lon": 121.56})
        assert client.get("/api/status").json()["session"]["state"] == "connected"
    # Leaving the context runs the lifespan shutdown.
    assert device.simulation.exited
    assert device.rsd.closed == 1


def test_shutdown_stops_a_running_route(device):
    app = create_app()
    with TestClient(app) as client:
        client.post("/api/connect", json={"udid": "UDID-1"})
        client.post(
            "/api/route/start",
            json={"points": [[25.0330, 121.5654], [25.0430, 121.5654]], "speed_kmh": 5.0},
        )
        assert client.get("/api/status").json()["route"]["running"] is True

    written = len(device.simulation.writes)
    # Nothing may keep moving after the server is gone.
    assert device.simulation.exited
    assert len(device.simulation.writes) == written
