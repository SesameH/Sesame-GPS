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
