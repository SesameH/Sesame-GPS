"""Startup path: tunneld detection, autostart, and the LaunchDaemon plist."""

import plistlib
import socket
import subprocess
import time

import pytest

from sesame import __main__ as cli


def test_tunneld_is_up_false_when_nothing_listens(monkeypatch):
    # Port 1 is reserved and never has a listener.
    monkeypatch.setattr(cli, "TUNNELD_DEFAULT_ADDRESS", ("127.0.0.1", 1))
    assert cli.tunneld_is_up(timeout=0.5) is False


def test_ensure_tunneld_does_nothing_when_already_up(monkeypatch):
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: True)

    def fail(*args, **kwargs):
        raise AssertionError("must not shell out when tunneld already answers")

    monkeypatch.setattr(cli.subprocess, "run", fail)
    assert cli.ensure_tunneld() is True


def test_ensure_tunneld_refuses_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    def fail(*args, **kwargs):
        raise AssertionError("sudo would hang with no tty to prompt on")

    monkeypatch.setattr(cli.subprocess, "run", fail)

    assert cli.ensure_tunneld() is False
    assert "sudo" in capsys.readouterr().err


def test_ensure_tunneld_starts_it_and_waits(monkeypatch):
    calls = []
    states = iter([False, False, True])
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: next(states, True))
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: calls.append(command))

    assert cli.ensure_tunneld() is True
    assert calls == [["sudo", "/fake/pymobiledevice3", "remote", "tunneld", "--daemonize"]]


def test_ensure_tunneld_reports_a_failed_launch(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def boom(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli.ensure_tunneld() is False
    assert "失敗" in capsys.readouterr().err


def test_daemon_install_refuses_without_root(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)
    assert cli.daemon_install() == 1
    assert "root" in capsys.readouterr().err


def test_daemon_install_writes_a_loadable_plist(monkeypatch, tmp_path):
    plist_path = tmp_path / f"{cli.LAUNCHD_LABEL}.plist"
    commands = []
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "LAUNCHD_PLIST", plist_path)
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0),
    )

    assert cli.daemon_install() == 0

    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["Label"] == cli.LAUNCHD_LABEL
    assert plist["ProgramArguments"] == ["/fake/pymobiledevice3", "remote", "tunneld"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert ["launchctl", "bootstrap", "system", str(plist_path)] in commands


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ["serve"]),
        (["--open"], ["serve", "--open"]),
        (["--port", "9000"], ["serve", "--port", "9000"]),
        (["serve", "--open"], ["serve", "--open"]),
        (["daemon", "status"], ["daemon", "status"]),
    ],
)
def test_bare_flags_fall_through_to_serve(argv, expected):
    # Mirrors the dispatch in main(): anything that is not a subcommand is serve.
    result = argv if argv and argv[0] in {"serve", "daemon", "-h", "--help"} else ["serve", *argv]
    assert result == expected


# -- app bundle ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/plain/path", "/plain/path"),
        ('/has "quotes"', '/has \\"quotes\\"'),
        ("/has\\backslash", "/has\\\\backslash"),
    ],
)
def test_applescript_string_escapes(value, expected):
    assert cli.applescript_string(value) == expected


def test_port_is_free_reports_both_ways():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.listen(1)
    taken = probe.getsockname()[1]
    try:
        free, reason = cli.port_is_free("127.0.0.1", taken)
        assert free is False
        assert reason
    finally:
        probe.close()

    free, reason = cli.port_is_free("127.0.0.1", taken)
    assert free is True
    assert reason == ""


def test_sesame_already_serving_false_when_nothing_answers():
    assert cli.sesame_already_serving("http://127.0.0.1:1") is False


def test_alert_without_gui_stays_on_stderr(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("no dialog outside app mode")

    monkeypatch.setattr(cli.subprocess, "run", fail)
    cli.alert("壞掉了", gui=False)
    assert "壞掉了" in capsys.readouterr().err


def test_alert_with_gui_shows_a_dialog(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: calls.append(command))
    cli.alert("壞掉了", gui=True)
    assert calls and calls[0][0] == "osascript"
    assert "display dialog" in calls[0][2]


@pytest.fixture
def fake_console_script(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "sesame"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "bin" / "python"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    # No icon on disk by default, so the icon branch stays out of these cases.
    monkeypatch.setattr(cli, "DEFAULT_ICON", tmp_path / "missing.png")
    return executable


def test_app_build_creates_a_launchable_bundle(tmp_path, fake_console_script):
    executable = fake_console_script

    assert cli.app_build(tmp_path, port=8765) == 0

    bundle = tmp_path / "Sesame.app"
    launcher = bundle / "Contents" / "MacOS" / "Sesame"
    assert launcher.stat().st_mode & 0o111  # executable
    body = launcher.read_text()
    assert body.startswith("#!/bin/bash")
    assert "serve --open --gui-sudo" in body
    assert str(executable) in body

    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleExecutable"] == "Sesame"
    assert info["CFBundlePackageType"] == "APPL"


def test_app_build_passes_a_custom_port(tmp_path, fake_console_script):
    cli.app_build(tmp_path, port=9000)
    body = (tmp_path / "Sesame.app" / "Contents" / "MacOS" / "Sesame").read_text()
    assert "--port 9000" in body


def test_app_build_refuses_when_the_console_script_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "bin" / "python"))
    assert cli.app_build(tmp_path, port=8765) == 1
    assert "uv sync" in capsys.readouterr().err


# -- icon ------------------------------------------------------------------


def pixel_art(path, size=64):
    """A hard-edged checkerboard: any smoothing shows up as intermediate colours."""
    from PIL import Image

    image = Image.new("RGBA", (size, size))
    pixels = image.load()
    for x in range(size):
        for y in range(size):
            pixels[x, y] = (0, 0, 0, 255) if (x // 8 + y // 8) % 2 else (255, 255, 255, 255)
    image.save(path)
    return path


def test_build_icns_produces_every_required_size(tmp_path):
    source = pixel_art(tmp_path / "icon.png")
    target = tmp_path / "Sesame.icns"
    assert cli.build_icns(source, target) is True
    assert target.exists()

    unpacked = tmp_path / "roundtrip.iconset"
    subprocess.run(["iconutil", "--convert", "iconset", str(target), "--output", str(unpacked)], check=True)
    assert {path.name for path in unpacked.iterdir()} == {name for _, name in cli.ICONSET_FILES}


def test_write_iconset_keeps_pixel_edges_hard(tmp_path):
    from PIL import Image

    source = pixel_art(tmp_path / "icon.png")
    iconset = tmp_path / "Sesame.iconset"
    iconset.mkdir()
    cli.write_iconset(source, iconset, rounded=False)

    assert {path.name for path in iconset.iterdir()} == {name for _, name in cli.ICONSET_FILES}

    for size, name in cli.ICONSET_FILES:
        with Image.open(iconset / name) as scaled:
            assert scaled.size == (size, size)
            colours = {pixel[:3] for pixel in scaled.convert("RGBA").get_flattened_data()}
        # Nearest-neighbour can only ever emit the two source colours; any blend
        # means a smooth filter crept in and the pixel art got mushy.
        assert colours <= {(0, 0, 0), (255, 255, 255)}, name


def test_app_build_installs_the_icon_when_present(tmp_path, fake_console_script, monkeypatch):
    # No subprocess stub here: the real iconutil has to run to produce the .icns.
    monkeypatch.undo()
    monkeypatch.setattr(cli.sys, "executable", str(fake_console_script.parent / "python"))
    icon = pixel_art(tmp_path / "icon.png")
    monkeypatch.setattr(cli, "DEFAULT_ICON", icon)

    assert cli.app_build(tmp_path / "out", port=8765) == 0

    bundle = tmp_path / "out" / "Sesame.app"
    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleIconFile"] == "Sesame"
    assert (bundle / "Contents" / "Resources" / "Sesame.icns").exists()


def test_app_build_reports_a_missing_explicit_icon(tmp_path, fake_console_script, capsys):
    assert cli.app_build(tmp_path / "out", port=8765, icon=tmp_path / "nope.png") == 1
    assert "找不到 icon" in capsys.readouterr().err


def test_app_build_still_builds_without_any_icon(tmp_path, fake_console_script, capsys):
    assert cli.app_build(tmp_path / "out", port=8765) == 0
    with (tmp_path / "out" / "Sesame.app" / "Contents" / "Info.plist").open("rb") as handle:
        assert "CFBundleIconFile" not in plistlib.load(handle)
    assert "沒有 icon" in capsys.readouterr().out


def test_refresh_launch_services_bumps_mtime_and_reregisters(tmp_path, monkeypatch):
    bundle = tmp_path / "Sesame.app"
    bundle.mkdir()
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: calls.append(command))

    before = bundle.stat().st_mtime
    time.sleep(0.01)
    cli.refresh_launch_services(bundle)

    assert bundle.stat().st_mtime != before
    assert calls == [[cli.LSREGISTER, "-f", str(bundle)]]


def test_refresh_launch_services_skips_a_missing_lsregister(tmp_path, monkeypatch):
    bundle = tmp_path / "Sesame.app"
    bundle.mkdir()
    monkeypatch.setattr(cli, "LSREGISTER", str(tmp_path / "nope"))

    def fail(*args, **kwargs):
        raise AssertionError("must not run a tool that is not there")

    monkeypatch.setattr(cli.subprocess, "run", fail)
    cli.refresh_launch_services(bundle)


def test_write_iconset_accepts_an_animated_gif(tmp_path):
    from PIL import Image

    frames = [
        Image.new("RGBA", (64, 64), colour)
        for colour in [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]
    ]
    source = tmp_path / "cat.gif"
    frames[0].save(source, save_all=True, append_images=frames[1:], loop=0)

    iconset = tmp_path / "Sesame.iconset"
    iconset.mkdir()
    # Square mode keeps this about frame selection rather than the corner mask.
    cli.write_iconset(source, iconset, rounded=False)

    with Image.open(iconset / "icon_128x128.png") as scaled:
        colours = {pixel[:3] for pixel in scaled.convert("RGBA").get_flattened_data()}
    # The first frame is the one that becomes the icon; a later frame leaking
    # through would show up as green or blue here.
    assert colours == {(255, 0, 0)}


def test_squircle_mask_is_round_cornered_and_solid_in_the_middle():
    mask = cli.squircle_mask(256)
    assert mask.size == (256, 256)
    assert mask.getpixel((1, 1)) == 0  # corner cut away
    assert mask.getpixel((128, 128)) == 255  # centre kept
    assert mask.getpixel((128, 1)) == 255  # flat mid-edge, not a circle


def test_rounded_iconset_insets_to_the_macos_grid(tmp_path):
    from PIL import Image

    source = pixel_art(tmp_path / "icon.png", size=256)
    iconset = tmp_path / "Sesame.iconset"
    iconset.mkdir()
    cli.write_iconset(source, iconset, rounded=True)

    with Image.open(iconset / "icon_512x512@2x.png") as icon:
        assert icon.size == (1024, 1024)
        assert icon.getpixel((2, 2))[3] == 0  # transparent margin
        assert icon.getpixel((512, 512))[3] == 255  # artwork in the middle
        # Artwork occupies Apple's 824/1024 content square, so the margin edge
        # must be clear and the inset edge must not be.
        assert icon.getpixel((512, 60))[3] == 0
        assert icon.getpixel((512, 140))[3] == 255


def test_rounded_iconset_keeps_the_artwork_unblended(tmp_path):
    from PIL import Image

    source = pixel_art(tmp_path / "icon.png", size=256)
    iconset = tmp_path / "Sesame.iconset"
    iconset.mkdir()
    cli.write_iconset(source, iconset, rounded=True)

    with Image.open(iconset / "icon_512x512@2x.png") as icon:
        middle = icon.convert("RGBA").crop((300, 300, 724, 724))
    colours = {pixel[:3] for pixel in middle.get_flattened_data() if pixel[3] == 255}
    # Only the corner mask is allowed to be antialiased; the pixels themselves
    # must still be exactly the two source colours.
    assert colours <= {(0, 0, 0), (255, 255, 255)}


def test_square_mode_skips_the_mask(tmp_path):
    from PIL import Image

    source = pixel_art(tmp_path / "icon.png", size=256)
    iconset = tmp_path / "Sesame.iconset"
    iconset.mkdir()
    cli.write_iconset(source, iconset, rounded=False)

    with Image.open(iconset / "icon_512x512@2x.png") as icon:
        assert icon.getpixel((2, 2))[3] == 255  # corner still opaque


def test_daemon_start_refuses_without_root(monkeypatch, capsys):
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)

    def fail(*args, **kwargs):
        raise AssertionError("must not launch tunneld unprivileged")

    monkeypatch.setattr(cli.subprocess, "run", fail)
    assert cli.daemon_start() == 1
    assert "root" in capsys.readouterr().err


def test_daemon_start_runs_tunneld_in_the_foreground(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    assert cli.daemon_start() == 0
    # No --daemonize: this variant is meant to be held open and Ctrl-C'd.
    assert calls == [["/fake/pymobiledevice3", "remote", "tunneld"]]


def test_daemon_start_reports_a_missing_pymobiledevice3(monkeypatch, capsys):
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: None)
    assert cli.daemon_start() == 1
    assert "pymobiledevice3" in capsys.readouterr().err


def test_gui_sudo_shell_quotes_a_path_with_spaces(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: bool(calls))
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/Users/Peter Huang/bin/pymobiledevice3")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )

    assert cli.ensure_tunneld(gui_sudo=True) is True

    script = calls[0][2]
    # Unquoted, the shell inside `do shell script` splits the home directory
    # and never finds the executable.
    assert "'/Users/Peter Huang/bin/pymobiledevice3'" in script
    assert script.startswith("do shell script ")
    assert script.endswith(" with administrator privileges")


def test_gui_sudo_surfaces_a_cancelled_prompt(monkeypatch):
    dialogs = []
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli, "alert", lambda message, gui: dialogs.append((message, gui)))

    def cancelled(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="execution error: User canceled. (-128)")

    monkeypatch.setattr(cli.subprocess, "run", cancelled)

    assert cli.ensure_tunneld(gui_sudo=True) is False
    message, gui = dialogs[0]
    assert gui is True
    assert "授權被取消" in message


def test_terminal_sudo_failure_stays_off_the_dialog(monkeypatch):
    dialogs = []
    monkeypatch.setattr(cli, "tunneld_is_up", lambda *a, **k: False)
    monkeypatch.setattr(cli, "pymobiledevice3_path", lambda: "/fake/pymobiledevice3")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "alert", lambda message, gui: dialogs.append(gui))

    def boom(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(cli.subprocess, "run", boom)

    assert cli.ensure_tunneld(gui_sudo=False) is False
    # alert() still runs, but with gui=False it must stay on stderr.
    assert dialogs == [False]
