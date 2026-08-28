"""Entry point: ``uv run sesame`` / ``python -m sesame``."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS

from sesame import __version__
from sesame.engine import pair_record_folder
from sesame.server import create_app

LAUNCHD_LABEL = "com.sesame.tunneld"
LAUNCHD_PLIST = Path("/Library/LaunchDaemons") / f"{LAUNCHD_LABEL}.plist"
LAUNCHD_LOG = "/var/log/sesame-tunneld.log"

TUNNELD_STARTUP_TIMEOUT = 30.0


def pymobiledevice3_path() -> str | None:
    """The ``pymobiledevice3`` console script, preferring the one in this venv."""
    alongside = Path(sys.executable).parent / "pymobiledevice3"
    if alongside.exists():
        return str(alongside)
    return shutil.which("pymobiledevice3")


def tunneld_is_up(timeout: float = 1.5) -> bool:
    host, port = TUNNELD_DEFAULT_ADDRESS
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def applescript_string(value: str) -> str:
    """Quote a value for embedding in an AppleScript double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ensure_tunneld(gui_sudo: bool = False) -> bool:
    """Start tunneld if it is not already answering.

    tunneld needs root to create the tun interface, so this shells out through
    ``sudo`` and daemonizes it. It deliberately outlives sesame: the tunnel is
    shared, slow to rebuild, and losing it on every app restart is exactly the
    friction this avoids.

    :param gui_sudo: ask for the password through the macOS authentication
        dialog instead of the terminal. Used by the ``.app`` bundle, which has
        no tty to prompt on.
    """
    if tunneld_is_up():
        return True

    executable = pymobiledevice3_path()
    if executable is None:
        print("找不到 pymobiledevice3 執行檔。請先 `uv sync`。", file=sys.stderr)
        return False

    if gui_sudo:
        print("tunneld 沒在跑，跳出授權對話框啟動…", flush=True)
        # Two levels of quoting: shlex for the shell that `do shell script`
        # spawns, then AppleScript escaping for the literal it sits inside. A
        # pipx install lives under the home directory, so an account name with
        # a space in it splits the path without the first one.
        command = f"{shlex.quote(executable)} remote tunneld --daemonize"
        launch = [
            "osascript",
            "-e",
            f'do shell script "{applescript_string(command)}" with administrator privileges',
        ]
    elif not sys.stdin.isatty():
        print(
            "tunneld 沒在跑，而且這裡不是終端機，沒辦法問 sudo 密碼。\n"
            f"請先自己開：sudo {executable} remote tunneld",
            file=sys.stderr,
        )
        return False
    else:
        print("tunneld 沒在跑，正在用 sudo 啟動（會問你密碼，只問這一次）…", flush=True)
        launch = ["sudo", executable, "remote", "tunneld", "--daemonize"]

    try:
        subprocess.run(launch, capture_output=gui_sudo, text=True, check=True)
    except subprocess.CalledProcessError as error:
        # osascript reports a cancelled password prompt as -128 on stderr; with
        # no terminal to read, that has to become a dialog or it is invisible.
        detail = (error.stderr or "").strip() or str(error)
        if "-128" in detail:
            detail = "授權被取消。"
        alert(f"啟動 tunneld 失敗。\n\n{detail}", gui_sudo)
        return False
    except KeyboardInterrupt:
        print("啟動 tunneld 被中斷。", file=sys.stderr)
        return False

    deadline = time.monotonic() + TUNNELD_STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if tunneld_is_up():
            print("tunneld 起來了。", flush=True)
            return True
        time.sleep(0.5)

    alert(
        f"tunneld 啟動後 {TUNNELD_STARTUP_TIMEOUT:.0f} 秒內沒有回應。\n\n"
        f"看看 {APP_LOG.replace('$HOME', str(Path.home()))} 有沒有線索。",
        gui_sudo,
    )
    return False


# -- launchd daemon --------------------------------------------------------


def daemon_install() -> int:
    """Install tunneld as a LaunchDaemon so it survives reboots."""
    if os.geteuid() != 0:
        print(f"要 root。請跑：sudo {sys.argv[0]} daemon install", file=sys.stderr)
        return 1

    executable = pymobiledevice3_path()
    if executable is None:
        print("找不到 pymobiledevice3 執行檔。", file=sys.stderr)
        return 1

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [executable, "remote", "tunneld"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": LAUNCHD_LOG,
        "StandardErrorPath": LAUNCHD_LOG,
    }
    with LAUNCHD_PLIST.open("wb") as handle:
        plistlib.dump(plist, handle)
    LAUNCHD_PLIST.chmod(0o644)

    # Replace any previous registration; bootout fails harmlessly when absent.
    subprocess.run(["launchctl", "bootout", f"system/{LAUNCHD_LABEL}"], capture_output=True, check=False)
    result = subprocess.run(["launchctl", "bootstrap", "system", str(LAUNCHD_PLIST)], check=False)
    if result.returncode != 0:
        print(f"launchctl bootstrap 失敗（代碼 {result.returncode}）。", file=sys.stderr)
        return result.returncode

    print(f"已安裝 {LAUNCHD_PLIST}。tunneld 現在開機就會自己跑，log 在 {LAUNCHD_LOG}。")
    return 0


def daemon_start() -> int:
    """Run tunneld in the foreground.

    For people who would rather manage the daemon themselves than let ``serve``
    start it. The point of routing it through here is that the
    ``pymobiledevice3`` console script lives inside whatever environment sesame
    was installed into -- for a pipx or uv install that is not on ``PATH``, so
    there is otherwise no obvious command to type.
    """
    executable = pymobiledevice3_path()
    if executable is None:
        print("找不到 pymobiledevice3 執行檔。", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        print(f"要 root。請跑：sudo {sys.argv[0]} daemon start", file=sys.stderr)
        return 1
    return subprocess.run([executable, "remote", "tunneld"], check=False).returncode


def daemon_uninstall() -> int:
    if os.geteuid() != 0:
        print(f"要 root。請跑：sudo {sys.argv[0]} daemon uninstall", file=sys.stderr)
        return 1

    subprocess.run(["launchctl", "bootout", f"system/{LAUNCHD_LABEL}"], capture_output=True, check=False)
    LAUNCHD_PLIST.unlink(missing_ok=True)
    print(f"已移除 {LAUNCHD_LABEL}。")
    return 0


def daemon_status() -> int:
    installed = LAUNCHD_PLIST.exists()
    print(f"LaunchDaemon: {'已安裝' if installed else '未安裝'}（{LAUNCHD_PLIST}）")
    print(
        f"tunneld:      {'有回應' if tunneld_is_up() else '沒有回應'}"
        f"（http://{TUNNELD_DEFAULT_ADDRESS[0]}:{TUNNELD_DEFAULT_ADDRESS[1]}）"
    )
    return 0


# -- pairing ---------------------------------------------------------------


def doctor() -> int:
    """Report whether everything Wi-Fi discovery depends on is in place.

    Each of these fails in the same way from the outside -- an empty device
    list -- so the point is to say which one it is.
    """
    import asyncio

    from sesame.engine import advertised_wifi_macs, stored_wifi_macs

    problems = 0

    if tunneld_is_up():
        print("✓ tunneld 有回應")
    else:
        problems += 1
        print("✗ tunneld 沒在跑。開 Sesame 會自動啟動，或跑 sudo sesame daemon start")

    if LAUNCHD_PLIST.exists():
        print("✓ LaunchDaemon 已安裝，Mac 重開機後 tunneld 會自己起來")
    else:
        print("· LaunchDaemon 未安裝：Mac 每次重開機都要重新授權一次")
        print("  裝起來就不用再輸密碼：sudo sesame daemon install")

    stored = stored_wifi_macs()
    advertised = asyncio.run(advertised_wifi_macs(4.0))

    if not stored:
        problems += 1
        print(f"✗ 沒有配對記錄（{pair_record_folder()}）—— WiFi 一定找不到裝置")
        print("  插上 USB 跑一次：sesame pair")
    elif stored.keys() & advertised:
        matched = ", ".join(sorted(stored.keys() & advertised))
        print(f"✓ 配對記錄對得上正在廣播的裝置（{matched}）")
    elif advertised:
        problems += 1
        print("✗ 配對記錄跟正在廣播的位址對不上 —— 手機換過私人 WiFi 位址了")
        print(f"  記錄裡：{', '.join(sorted(stored))}")
        print(f"  廣播中：{', '.join(sorted(advertised))}")
        print("  插上 USB 重新配對一次：sesame pair")
    else:
        print("· 這個網路上沒有裝置在廣播。手機解鎖了嗎？跟這台在同一個 WiFi 嗎？")

    return 1 if problems else 0


def pair() -> int:
    """Pair over USB, writing the record Wi-Fi discovery needs."""
    import asyncio

    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices

    async def run() -> int:
        devices = [device for device in await list_devices() if device.connection_type == "USB"]
        if not devices:
            print(
                "沒有偵測到用 USB 連著的裝置。\n配對必須走傳輸線 —— 這正是之後 WiFi 才找得到它的原因。",
                file=sys.stderr,
            )
            return 1

        for device in devices:
            lockdown = await create_using_usbmux(serial=device.serial)
            async with lockdown:
                print(f"配對 {device.serial}…請看手機螢幕上的「信任這台電腦」。", flush=True)
                await lockdown.pair()
                print(f"完成。記錄寫在 {pair_record_folder()}", flush=True)
        return 0

    try:
        return asyncio.run(run())
    except Exception as error:
        print(f"配對失敗：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


# -- .app bundle -----------------------------------------------------------

APP_NAME = "Sesame"
APP_BUNDLE_ID = "com.sesame.app"
APP_LOG = "$HOME/Library/Logs/sesame.log"

LAUNCHER_TEMPLATE = """#!/bin/bash
# Generated by `sesame app build`. Rebuild after moving the project.
exec >>"{log}" 2>&1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') 啟動 sesame"
exec {executable} serve --open --gui-sudo --quit-on-close
"""


# macOS wants every one of these; iconutil rejects an incomplete iconset.
ICONSET_FILES = (
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
)

# Inside the package so it survives an install, not just a source checkout.
DEFAULT_ICON = Path(__file__).parent / "assets" / "icon.png"

# Apple's macOS icon grid: the artwork fills 824 of a 1024 canvas, the rest is
# transparent margin. Matching it is what makes an icon sit at the same visual
# size as everything else in the Dock.
ICON_CONTENT_RATIO = 824 / 1024
# macOS corners are a superellipse, not a rounded rectangle; n=5 is the shape
# Apple's own icons follow.
SQUIRCLE_EXPONENT = 5.0


def squircle_mask(size: int, supersample: int = 4):
    """An antialiased macOS-style rounded-corner mask.

    Drawn as a polygon at several times the target resolution and scaled down,
    which is both far faster than testing the superellipse per pixel and gives
    the corners clean antialiasing.
    """
    from PIL import Image, ImageDraw

    resolution = size * supersample
    radius = resolution / 2
    points = []
    for step in range(720):
        angle = 2 * math.pi * step / 720
        cosine, sine = math.cos(angle), math.sin(angle)
        x = math.copysign(abs(cosine) ** (2 / SQUIRCLE_EXPONENT), cosine)
        y = math.copysign(abs(sine) ** (2 / SQUIRCLE_EXPONENT), sine)
        points.append((radius + x * (radius - 1), radius + y * (radius - 1)))

    mask = Image.new("L", (resolution, resolution), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def write_iconset(source: Path, iconset: Path, rounded: bool = True) -> None:
    """Fill an ``.iconset`` directory with every size macOS asks for.

    Scaling is nearest-neighbour on purpose: the icon is pixel art, and every
    smooth filter (``sips`` included) turns its hard edges into mush. The
    corner mask is the one thing that *is* antialiased -- a hard-edged
    superellipse would look ragged at small sizes.
    """
    from PIL import Image, ImageChops

    with Image.open(source) as image:
        square = image.convert("RGBA")
        for size, name in ICONSET_FILES:
            if not rounded:
                square.resize((size, size), Image.Resampling.NEAREST).save(iconset / name)
                continue

            inner = max(1, round(size * ICON_CONTENT_RATIO))
            art = square.resize((inner, inner), Image.Resampling.NEAREST)
            art.putalpha(ImageChops.multiply(art.getchannel("A"), squircle_mask(inner)))

            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            offset = (size - inner) // 2
            canvas.paste(art, (offset, offset))
            canvas.save(iconset / name)


def build_icns(source: Path, destination: Path, rounded: bool = True) -> bool:
    """Convert a square image into the bundle's ``.icns``."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("轉 icon 需要 Pillow。跑 `uv sync` 之後再試。", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory() as workspace:
        iconset = Path(workspace) / f"{APP_NAME}.iconset"
        iconset.mkdir()
        write_iconset(source, iconset, rounded=rounded)

        result = subprocess.run(
            ["iconutil", "--convert", "icns", str(iconset), "--output", str(destination)],
            capture_output=True,
            text=True,
            check=False,
        )

    if result.returncode != 0:
        print(f"iconutil 失敗：{result.stderr.strip()}", file=sys.stderr)
        return False
    return True


LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
)


def refresh_launch_services(bundle: Path) -> None:
    """Make Finder notice a rebuilt bundle straight away.

    Finder caches icons per bundle, so a rebuild that only swaps the artwork
    otherwise keeps showing the old (or default) icon until something evicts
    the cache. Bumping the mtime and re-registering with Launch Services does
    that without the usual ``killall Finder`` sledgehammer.
    """
    bundle.touch()
    if Path(LSREGISTER).exists():
        subprocess.run([LSREGISTER, "-f", str(bundle)], check=False, capture_output=True)


def app_build(destination: Path, port: int, icon: Path | None = None, rounded: bool = True) -> int:
    """Generate a double-clickable ``.app`` that launches the server.

    The bundle is a launcher, not a frozen copy: it points at this checkout's
    console script, so the project must stay where it is. That keeps the build
    instant and avoids shipping pymobiledevice3's native dependencies, which
    would need signing and notarisation to run on another Mac anyway.
    """
    executable = Path(sys.executable).parent / "sesame"
    if not executable.exists():
        print(f"找不到 {executable}。請先 `uv sync`。", file=sys.stderr)
        return 1

    bundle = destination / f"{APP_NAME}.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)

    launcher = macos / APP_NAME
    command = shlex.quote(str(executable))
    if port != 8765:
        command += f" --port {port}"
    launcher.write_text(LAUNCHER_TEMPLATE.format(log=APP_LOG, executable=command))
    launcher.chmod(0o755)

    icon_source = icon or DEFAULT_ICON
    icon_installed = icon_source.exists() and build_icns(
        icon_source, bundle / "Contents" / "Resources" / f"{APP_NAME}.icns", rounded=rounded
    )
    if icon is not None and not icon_source.exists():
        print(f"找不到 icon：{icon_source}", file=sys.stderr)
        return 1

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
    }
    if icon_installed:
        info["CFBundleIconFile"] = APP_NAME
    with (bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)

    refresh_launch_services(bundle)

    print(f"已產生 {bundle}")
    if not icon_installed:
        print(f"（沒有 icon，把 PNG 放到 {DEFAULT_ICON} 再跑一次就會套上）")
    print(f"雙擊開啟。log 在 {APP_LOG.replace('$HOME', str(Path.home()))}")
    return 0


# -- serve -----------------------------------------------------------------


def alert(message: str, gui: bool) -> None:
    """Report a fatal startup problem where the user will actually see it.

    Launched from the ``.app`` there is no terminal, so stderr goes to a log
    file nobody reads; a dialog is the only visible channel.
    """
    print(message, file=sys.stderr, flush=True)
    if gui:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display dialog "{applescript_string(message)}" '
                'with title "Sesame" buttons {"OK"} default button 1 with icon caution',
            ],
            check=False,
            capture_output=True,
        )


def sesame_already_serving(url: str, attempts: int = 3, timeout: float = 2.5) -> bool:
    """Whether the address is answering as a sesame instance rather than something else.

    Retried, because an instance that is still starting up answers late and
    being wrong here turns into a "port is taken" dialog for what is really
    just the app the user already had open.
    """
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=timeout) as response:
                return "session" in json.load(response)
        except (urllib.error.URLError, OSError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(0.5)
    return False


def port_is_free(host: str, port: int) -> tuple[bool, str]:
    """Probe the address by binding it, reporting why if that fails."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Match uvicorn, which sets this before binding; without it the probe can
    # refuse a port that uvicorn would have taken happily.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError as error:
        return False, str(error)
    finally:
        probe.close()
    return True, ""


def port_holder(port: int) -> str | None:
    """Name of the process holding a port, for a message the user can act on."""
    try:
        listed = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, check=False)
        pids = listed.stdout.split()
        if not pids:
            return None
        described = subprocess.run(
            ["ps", "-o", "comm=", "-p", pids[0]], capture_output=True, text=True, check=False
        )
        # `ps -o comm=` gives a full path on macOS; the basename is what reads.
        name = Path(described.stdout.strip()).name
        return f"{name} (pid {pids[0]})" if name else f"pid {pids[0]}"
    except OSError:
        return None


def serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    url = f"http://{args.host}:{args.port}"

    # Double-clicking a second time should land on the running instance rather
    # than dying on a port collision the user never sees.
    if sesame_already_serving(url):
        print(f"已經有一個 sesame 在 {url}，直接開過去。", flush=True)
        webbrowser.open(url)
        return 0

    if args.tunneld and not ensure_tunneld(gui_sudo=args.gui_sudo):
        print("沒有 tunneld 就抓不到裝置，但介面還是會開著。", file=sys.stderr)

    # uvicorn swallows a bind failure and calls sys.exit(), so the collision has
    # to be caught here to be reportable.
    free, reason = port_is_free(args.host, args.port)
    if not free:
        # Starting tunneld can take half a minute, which is long enough for an
        # instance to have finished starting since the check at the top.
        if sesame_already_serving(url):
            print(f"已經有一個 sesame 在 {url}，直接開過去。", flush=True)
            webbrowser.open(url)
            return 0
        holder = port_holder(args.port)
        blame = f"佔用它的是 {holder}。" if holder else reason
        alert(
            f"開不起來：{args.host}:{args.port} 被別的程式佔用了。\n\n{blame}\n\n"
            f"換一個連接埠：sesame --port 8766",
            args.gui_sudo,
        )
        return 1

    print(f"sesame listening on {url}", flush=True)
    if args.open:
        webbrowser.open(url)

    # Built by hand rather than via uvicorn.run so the app can ask the server
    # to stop when the last browser goes away.
    def request_shutdown() -> None:
        server.should_exit = True

    app = create_app(on_idle=request_shutdown if args.quit_on_close else None)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    server.run()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="sesame", description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="啟動網頁介面（預設）")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--open", action="store_true", help="啟動後自動開瀏覽器")
    serve_parser.add_argument(
        "--no-tunneld",
        dest="tunneld",
        action="store_false",
        help="不要自動啟動 tunneld（自己另外開）",
    )
    serve_parser.add_argument(
        "--gui-sudo",
        action="store_true",
        help="用 macOS 授權對話框而不是終端機問 sudo 密碼（.app 用）",
    )
    serve_parser.add_argument(
        "--quit-on-close",
        action="store_true",
        help="關掉最後一個瀏覽器分頁就結束 server（.app 預設開啟）",
    )
    serve_parser.add_argument("--verbose", action="store_true")

    daemon_parser = subparsers.add_parser("daemon", help="把 tunneld 裝成開機自動啟動的服務")
    daemon_parser.add_argument("action", choices=["start", "install", "uninstall", "status"])

    subparsers.add_parser("pair", help="用 USB 配對一次，之後 WiFi 才找得到裝置")
    subparsers.add_parser("doctor", help="檢查 WiFi 探索需要的每個環節")

    app_parser = subparsers.add_parser("app", help="產生可雙擊開啟的 .app")
    app_parser.add_argument("action", choices=["build"])
    app_parser.add_argument(
        "--dest",
        type=Path,
        default=Path.home() / "Applications",
        help="放 .app 的資料夾（預設 ~/Applications）",
    )
    app_parser.add_argument("--port", type=int, default=8765)
    app_parser.add_argument(
        "--icon",
        type=Path,
        default=None,
        help=f"icon 用的方形圖（預設 {DEFAULT_ICON}，動畫 GIF 取第一格）",
    )
    app_parser.add_argument(
        "--square",
        dest="rounded",
        action="store_false",
        help="不要套 macOS 圓角，直接用原圖的方形",
    )

    # Bare `sesame --open` keeps working: fall through to the serve subcommand.
    argv = sys.argv[1:]
    if not argv or argv[0] not in {"serve", "daemon", "app", "pair", "doctor", "-h", "--help"}:
        argv = ["serve", *argv]
    args = parser.parse_args(argv)

    if args.command == "pair":
        raise SystemExit(pair())

    if args.command == "doctor":
        raise SystemExit(doctor())

    if args.command == "app":
        args.dest.mkdir(parents=True, exist_ok=True)
        raise SystemExit(app_build(args.dest, args.port, args.icon, args.rounded))

    if args.command == "daemon":
        raise SystemExit(
            {
                "start": daemon_start,
                "install": daemon_install,
                "uninstall": daemon_uninstall,
                "status": daemon_status,
            }[args.action]()
        )

    raise SystemExit(serve(args))


if __name__ == "__main__":
    main()
