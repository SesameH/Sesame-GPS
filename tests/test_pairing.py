"""Pairing and network discovery, with the pymobiledevice3 layer faked out."""

import plistlib

import pytest

from sesame import engine
from sesame.engine import PairingError, pair_over_usb


class FakeMuxDevice:
    def __init__(self, serial, connection_type="USB"):
        self.serial = serial
        self.connection_type = connection_type


class FakeLockdown:
    def __init__(self, record, folder, serial, on_pair=None):
        self.pair_record = record
        self._folder = folder
        self._serial = serial
        self._on_pair = on_pair
        self.paired = 0
        self.saved = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def pair(self):
        self.paired += 1
        self.pair_record = {"WiFiMACAddress": "60:57:c8:92:6f:72"}
        if self._on_pair is not None:
            self._on_pair()

    async def save_pair_record(self):
        self.saved += 1
        if self.pair_record is not None:
            (self._folder / f"{self._serial}.plist").write_bytes(plistlib.dumps(self.pair_record))


@pytest.fixture
def usb(tmp_path, monkeypatch):
    """A device on the cable, with the pair-record folder redirected."""
    import pymobiledevice3.lockdown as lockdown_module
    import pymobiledevice3.usbmux as usbmux_module

    state = {"devices": [FakeMuxDevice("UDID-1")], "record": None, "lockdowns": []}
    monkeypatch.setattr(engine, "pair_record_folder", lambda: tmp_path)

    async def fake_list_devices():
        return state["devices"]

    async def fake_create(serial, **kwargs):
        made = FakeLockdown(state["record"], tmp_path, serial)
        state["lockdowns"].append(made)
        return made

    monkeypatch.setattr(usbmux_module, "list_devices", fake_list_devices)
    monkeypatch.setattr(lockdown_module, "create_using_usbmux", fake_create)
    state["folder"] = tmp_path
    return state


async def test_pairing_needs_a_cable(usb):
    usb["devices"] = []
    with pytest.raises(PairingError, match="USB"):
        await pair_over_usb()


async def test_a_network_device_does_not_count_as_a_cable(usb):
    # usbmuxd also reports Wi-Fi-synced devices; pairing has to be over USB.
    usb["devices"] = [FakeMuxDevice("UDID-1", connection_type="Network")]
    with pytest.raises(PairingError, match="USB"):
        await pair_over_usb()


async def test_an_existing_record_is_copied_rather_than_repaired(usb):
    # The usual case: trusted through Finder, so the record exists already and
    # merely has to be written where Wi-Fi discovery will look.
    usb["record"] = {"WiFiMACAddress": "60:57:c8:92:6f:72"}
    results = await pair_over_usb()

    assert results == [
        {
            "udid": "UDID-1",
            "wifiMac": "60:57:c8:92:6f:72",
            "record": str(usb["folder"] / "UDID-1.plist"),
        }
    ]
    assert usb["lockdowns"][0].paired == 0
    assert usb["lockdowns"][0].saved == 1


async def test_no_record_means_a_real_pairing(usb):
    usb["record"] = None
    results = await pair_over_usb()
    assert usb["lockdowns"][0].paired == 1
    assert results[0]["wifiMac"] == "60:57:c8:92:6f:72"


async def test_a_record_without_a_wifi_mac_is_re_paired(usb):
    # Such a record cannot ever match a Bonjour advertisement, so copying it
    # would report success on something that can never work.
    usb["record"] = {"HostID": "x"}
    await pair_over_usb()
    assert usb["lockdowns"][0].paired == 1


async def test_a_record_that_never_lands_is_an_error(usb, monkeypatch):
    usb["record"] = {"WiFiMACAddress": "60:57:c8:92:6f:72"}

    async def save_nothing(self):
        self.saved += 1

    monkeypatch.setattr(FakeLockdown, "save_pair_record", save_nothing)
    with pytest.raises(PairingError, match="找不到記錄檔"):
        await pair_over_usb()


async def test_every_attached_device_gets_a_record(usb):
    usb["devices"] = [FakeMuxDevice("UDID-1"), FakeMuxDevice("UDID-2")]
    usb["record"] = {"WiFiMACAddress": "60:57:c8:92:6f:72"}
    results = await pair_over_usb()
    assert [entry["udid"] for entry in results] == ["UDID-1", "UDID-2"]


# -- discovery -------------------------------------------------------------


class FakeService:
    def __init__(self, identifier):
        self.remote_identifier = identifier
        self.closed = False

    async def close(self):
        self.closed = True


async def test_discoverable_udids_collects_and_closes(monkeypatch):
    import pymobiledevice3.remote.tunnel_service as tunnel_module

    services = [FakeService("UDID-1"), FakeService("UDID-1"), FakeService("UDID-2")]

    async def fake_services():
        return services

    monkeypatch.setattr(tunnel_module, "get_remote_pairing_tunnel_services", fake_services)
    assert await engine.discoverable_udids() == {"UDID-1", "UDID-2"}
    # Left open, these leak a socket per scan.
    assert all(service.closed for service in services)


async def test_discoverable_udids_survives_a_browse_failure(monkeypatch):
    import pymobiledevice3.remote.tunnel_service as tunnel_module

    async def broken():
        raise OSError("no route to host")

    monkeypatch.setattr(tunnel_module, "get_remote_pairing_tunnel_services", broken)
    assert await engine.discoverable_udids() == set()


async def test_discoverable_udids_gives_up_rather_than_hanging(monkeypatch):
    import asyncio

    import pymobiledevice3.remote.tunnel_service as tunnel_module

    async def never():
        await asyncio.sleep(60)

    monkeypatch.setattr(tunnel_module, "get_remote_pairing_tunnel_services", never)
    assert await engine.discoverable_udids(timeout=-19.9) == set()


def test_device_properties_without_a_handshake():
    class Bare:
        peer_info = None

    assert engine.device_properties(Bare()) == {}
