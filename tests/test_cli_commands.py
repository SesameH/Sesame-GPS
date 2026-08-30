"""The command surface: dispatch, the daemon actions, doctor, and serve."""

import subprocess

import pytest

from sesame import __main__ as cli


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)


@pytest.fixture
def quiet_tunneld(monkeypatch):
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)


# -- daemon ----------------------------------------------------------------


def test_daemon_restart_refuses_without_root(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)
    assert cli.daemon_restart() == 1
    assert "root" in capsys.readouterr().err


def test_daemon_restart_kills_then_starts(as_root, quiet_tunneld, monkeypatch):
    calls = []
    # Down after the kill, up again after the start.
    states = iter([True, False, False, True])
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: next(states, True))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(cli.time, "monotonic", _ticking())

    assert cli.daemon_restart() == 0
    assert calls[0][:2] == ["pkill", "-f"]
    assert calls[1] == ["/fake/pymobiledevice3", "remote", "tunneld", "--daemonize"]


def test_daemon_restart_reports_a_daemon_that_never_returns(as_root, quiet_tunneld, monkeypatch, capsys):
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "monotonic", _ticking(step=5.0))

    assert cli.daemon_restart() == 1
    assert "沒有回應" in capsys.readouterr().err


def test_daemon_restart_needs_the_executable(as_root, monkeypatch, capsys):
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    assert cli.daemon_restart() == 1
    assert "pymobiledevice3" in capsys.readouterr().err


def test_daemon_uninstall_refuses_without_root(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)
    assert cli.daemon_uninstall() == 1
    assert "root" in capsys.readouterr().err


def test_daemon_uninstall_removes_the_plist(as_root, tmp_path, monkeypatch):
    plist = tmp_path / "com.sesame.tunneld.plist"
    plist.write_text("x")
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)

    assert cli.daemon_uninstall() == 0
    assert not plist.exists()


def test_daemon_uninstall_is_idempotent(as_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "absent.plist")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    assert cli.daemon_uninstall() == 0


def test_daemon_status_reports_both_facts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "absent.plist")
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: True)
    assert cli.daemon_status() == 0
    printed = capsys.readouterr().out
    assert "未安裝" in printed
    assert "有回應" in printed


def test_daemon_install_reports_a_failing_launchctl(as_root, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "com.sesame.tunneld.plist")
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1))
    assert cli.daemon_install() == 1
    assert "bootstrap" in capsys.readouterr().err


def test_daemon_install_needs_the_executable(as_root, monkeypatch, capsys):
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: None)
    assert cli.daemon_install() == 1
    assert "pymobiledevice3" in capsys.readouterr().err


# -- pair ------------------------------------------------------------------


def test_pair_prints_what_landed(monkeypatch, capsys):
    from sesame import engine

    async def paired():
        return [{"udid": "UDID-1", "wifiMac": "60:57:c8:92:6f:72", "record": "/tmp/x.plist"}]

    monkeypatch.setattr(engine, "pair_over_usb", paired)
    assert cli.pair() == 0
    printed = capsys.readouterr().out
    assert "60:57:c8:92:6f:72" in printed
    assert "可以拔線" in printed


def test_pair_reports_a_pairing_error(monkeypatch, capsys):
    from sesame import engine

    async def refuse():
        raise engine.PairingError("no-cable", "No device found on USB.")

    monkeypatch.setattr(engine, "pair_over_usb", refuse)
    assert cli.pair() == 1
    assert "USB" in capsys.readouterr().err


def test_pair_reports_an_unexpected_failure(monkeypatch, capsys):
    from sesame import engine

    async def broken():
        raise ValueError("device went away")

    monkeypatch.setattr(engine, "pair_over_usb", broken)
    assert cli.pair() == 1
    assert "ValueError" in capsys.readouterr().err


# -- doctor ----------------------------------------------------------------


@pytest.fixture
def doctor_world(monkeypatch, tmp_path):
    from sesame import engine

    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "absent.plist")
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: True)
    monkeypatch.setattr(engine, "tunneld_tunnel_count", lambda: 0)
    monkeypatch.setattr(engine, "stored_wifi_macs", dict)

    async def nothing():
        return set()

    monkeypatch.setattr(engine, "advertised_wifi_macs", lambda timeout=3.0: nothing())
    monkeypatch.setattr(engine, "discoverable_udids", lambda timeout=4.0: nothing())
    return monkeypatch


def test_doctor_is_clean_when_a_tunnel_exists(doctor_world, capsys):
    from sesame import engine

    doctor_world.setattr(engine, "tunneld_tunnel_count", lambda: 1)
    assert cli.doctor() == 0
    assert "WiFi 探索正常運作" in capsys.readouterr().out


def test_doctor_reports_a_dead_daemon(doctor_world, capsys):
    doctor_world.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    assert cli.doctor() == 1
    assert "tunneld 沒在跑" in capsys.readouterr().out


def test_doctor_reports_a_stuck_daemon(doctor_world, capsys):
    from sesame import engine

    async def reachable():
        return {"UDID-1"}

    doctor_world.setattr(engine, "discoverable_udids", lambda timeout=4.0: reachable())
    assert cli.doctor() == 1
    assert "卡住" in capsys.readouterr().out


def test_doctor_reports_a_missing_pair_record(doctor_world, capsys):
    assert cli.doctor() == 1
    assert "沒有配對記錄" in capsys.readouterr().out


def test_doctor_names_the_private_address_mismatch(doctor_world, capsys):
    from sesame import engine

    doctor_world.setattr(engine, "stored_wifi_macs", lambda: {"60:57:c8:92:6f:72": "UDID-1"})

    async def advertising():
        return {"0e:98:d2:bb:d3:15"}

    doctor_world.setattr(engine, "advertised_wifi_macs", lambda timeout=3.0: advertising())
    assert cli.doctor() == 1
    printed = capsys.readouterr().out
    assert "重新配對沒有用" in printed


def test_doctor_notes_an_installed_daemon(doctor_world, tmp_path, capsys):
    from sesame import engine

    plist = tmp_path / "com.sesame.tunneld.plist"
    plist.write_text("x")
    doctor_world.setattr(cli, "LAUNCHD_PLIST", plist)
    doctor_world.setattr(engine, "tunneld_tunnel_count", lambda: 1)
    cli.doctor()
    assert "已安裝" in capsys.readouterr().out


# -- serve -----------------------------------------------------------------


def _args(**overrides):
    import argparse

    defaults = {
        "host": "127.0.0.1",
        "port": 8765,
        "open": False,
        "tunneld": False,
        "gui_sudo": False,
        "quit_on_close": False,
        "verbose": False,
    }
    return argparse.Namespace(**{**defaults, **overrides})


def test_serve_hands_off_to_a_running_instance(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(cli, "sesame_already_serving", lambda url, **k: True)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    def fail(*args, **kwargs):
        raise AssertionError("must not start a second server")

    monkeypatch.setattr(cli.uvicorn, "Server", fail)

    assert cli.serve(_args()) == 0
    assert opened == ["http://127.0.0.1:8765"]


def test_serve_refuses_a_port_held_by_something_else(monkeypatch, capsys):
    monkeypatch.setattr(cli, "sesame_already_serving", lambda url, **k: False)
    monkeypatch.setattr(cli, "port_is_free", lambda host, port: (False, "in use"))
    monkeypatch.setattr(cli, "port_holder", lambda port: "Safari (pid 1)")

    assert cli.serve(_args()) == 1
    assert "Safari (pid 1)" in capsys.readouterr().err


def test_serve_runs_the_server(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "sesame_already_serving", lambda url, **k: False)
    monkeypatch.setattr(cli, "port_is_free", lambda host, port: (True, ""))

    class FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self):
            started.append(True)

    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
    assert cli.serve(_args()) == 0
    assert started == [True]


def test_serve_warns_when_the_tunnel_will_not_start(monkeypatch, capsys):
    monkeypatch.setattr(cli, "sesame_already_serving", lambda url, **k: False)
    monkeypatch.setattr(cli, "port_is_free", lambda host, port: (True, ""))
    monkeypatch.setattr(cli, "ensure_tunneld", lambda gui_sudo=False: False)

    class FakeServer:
        def __init__(self, config):
            pass

        def run(self):
            pass

    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
    # The interface still opens; it just cannot find devices.
    assert cli.serve(_args(tunneld=True)) == 0
    assert "抓不到裝置" in capsys.readouterr().err


def _ticking(step=0.1):
    """A monotonic clock that always advances, so timeout loops terminate."""
    state = {"now": 0.0}

    def now():
        state["now"] += step
        return state["now"]

    return now


# -- dispatch --------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["sesame"], "serve"),
        (["sesame", "--open"], "serve"),
        (["sesame", "--port", "9000"], "serve"),
        (["sesame", "serve"], "serve"),
        (["sesame", "pair"], "pair"),
        (["sesame", "doctor"], "doctor"),
        (["sesame", "daemon", "start"], "daemon:start"),
        (["sesame", "daemon", "restart"], "daemon:restart"),
        (["sesame", "daemon", "install"], "daemon:install"),
        (["sesame", "daemon", "uninstall"], "daemon:uninstall"),
        (["sesame", "daemon", "status"], "daemon:status"),
        (["sesame", "daemon", "watchdog"], "daemon:watchdog"),
        (["sesame", "app", "build"], "app:build"),
    ],
)
def test_main_dispatches_every_command(argv, expected, monkeypatch):
    """Each command must reach its handler, not die in argument parsing."""
    reached = []
    monkeypatch.setattr(cli.sys, "argv", argv)
    monkeypatch.setattr(cli, "serve", lambda args: reached.append("serve") or 0)
    monkeypatch.setattr(cli, "daemon_install", lambda *a: reached.append("daemon:install") or 0)
    monkeypatch.setattr(cli, "pair", lambda: reached.append("pair") or 0)
    monkeypatch.setattr(cli, "doctor", lambda: reached.append("doctor") or 0)
    monkeypatch.setattr(cli, "app_build", lambda *a, **k: reached.append("app:build") or 0)
    for action in ("start", "restart", "uninstall", "status", "watchdog"):
        monkeypatch.setattr(
            cli,
            f"daemon_{action}",
            (lambda name: lambda: reached.append(f"daemon:{name}") or 0)(action),
        )

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0
    assert reached == [expected]


def test_main_rejects_an_unknown_daemon_action(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["sesame", "daemon", "nonsense"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2


# -- watchdog --------------------------------------------------------------


@pytest.fixture
def watchdog_world(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "WATCHDOG_STAMP", tmp_path / "stamp")
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: True)
    monkeypatch.setattr(cli, "tunneld_tunnel_count", lambda: 0)

    async def reachable():
        return {"UDID-1"}

    monkeypatch.setattr(cli, "discoverable_udids", lambda timeout=4.0: reachable())
    restarts = []
    monkeypatch.setattr(cli, "daemon_restart", lambda: restarts.append(True) or 0)
    return monkeypatch, restarts


def test_watchdog_restarts_a_stuck_daemon(watchdog_world):
    _, restarts = watchdog_world
    assert cli.daemon_watchdog() == 0
    assert restarts == [True]


def test_watchdog_leaves_a_working_daemon_alone(watchdog_world):
    monkeypatch, restarts = watchdog_world
    monkeypatch.setattr(cli, "tunneld_tunnel_count", lambda: 1)
    assert cli.daemon_watchdog() == 0
    assert restarts == []


def test_watchdog_leaves_a_dead_daemon_to_launchd(watchdog_world):
    monkeypatch, restarts = watchdog_world
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    # KeepAlive already restarts a daemon that exited; racing it would churn.
    assert cli.daemon_watchdog() == 0
    assert restarts == []


def test_watchdog_does_nothing_with_no_device_around(watchdog_world):
    monkeypatch, restarts = watchdog_world

    async def nothing():
        return set()

    monkeypatch.setattr(cli, "discoverable_udids", lambda timeout=4.0: nothing())
    # Holding no tunnels is correct when there is nothing to reach.
    assert cli.daemon_watchdog() == 0
    assert restarts == []


def test_watchdog_honours_its_cooldown(watchdog_world):
    _, restarts = watchdog_world
    assert cli.daemon_watchdog() == 0
    assert cli.daemon_watchdog() == 0
    # A restart takes time to help; a tighter loop would just churn.
    assert restarts == [True]


def test_watchdog_ignores_an_expired_stamp(watchdog_world, monkeypatch):
    _, restarts = watchdog_world
    cli.daemon_watchdog()
    later = cli.time.time() + cli.WATCHDOG_COOLDOWN + 1
    monkeypatch.setattr(cli.time, "time", lambda: later)
    cli.daemon_watchdog()
    assert restarts == [True, True]


def test_install_adds_both_daemons(as_root, tmp_path, monkeypatch):
    import plistlib

    commands = []
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "com.sesame.tunneld.plist")
    monkeypatch.setattr(cli, "WATCHDOG_PLIST", tmp_path / "com.sesame.watchdog.plist")
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    executable = tmp_path / "bin" / "sesame"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "bin" / "python"))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0),
    )

    assert cli.daemon_install() == 0

    with (tmp_path / "com.sesame.watchdog.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"] == [str(executable), "daemon", "watchdog"]
    assert plist["StartInterval"] == cli.WATCHDOG_INTERVAL
    assert ["launchctl", "bootstrap", "system", str(tmp_path / "com.sesame.watchdog.plist")] in commands


def test_install_can_skip_the_watchdog(as_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tmp_path / "com.sesame.tunneld.plist")
    monkeypatch.setattr(cli, "WATCHDOG_PLIST", tmp_path / "com.sesame.watchdog.plist")
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0))

    assert cli.daemon_install(watchdog=False) == 0
    assert not (tmp_path / "com.sesame.watchdog.plist").exists()


def test_uninstall_removes_both(as_root, tmp_path, monkeypatch):
    tunneld = tmp_path / "com.sesame.tunneld.plist"
    watchdog = tmp_path / "com.sesame.watchdog.plist"
    tunneld.write_text("x")
    watchdog.write_text("x")
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", tunneld)
    monkeypatch.setattr(cli, "WATCHDOG_PLIST", watchdog)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)

    assert cli.daemon_uninstall() == 0
    assert not tunneld.exists()
    assert not watchdog.exists()
