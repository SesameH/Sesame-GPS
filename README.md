# sesame

Simulate your iPhone's GPS location from a map in your browser. Click to teleport, or draw a
route and walk it at a real, constant speed.

Built on [pymobiledevice3](https://github.com/doronz88/pymobiledevice3). macOS only.

## Requirements

- macOS
- An iPhone or iPad running **iOS 17 or later**, with Developer Mode enabled
  (Settings → Privacy & Security → Developer Mode)
- Python 3.13 or newer (macOS ships 3.9 — see [Install](#install))

The device must be reachable from this Mac — over USB, or over Wi-Fi once it has been paired.

## Install

```bash
pipx install git+https://github.com/SesameH/Sesame-GPS.git
```

`sesame` is then on your `PATH`. Upgrade later with `pipx upgrade sesame`, remove it with
`pipx uninstall sesame`.

### If you don't have pipx

pipx is not pip. pip installs libraries into whatever Python environment is active; pipx installs
*applications*, each into its own isolated environment, and puts their commands on your `PATH`.
That matters here — this app pulls in around a hundred dependencies, and you do not want those in
your system Python.

You also need a **Python 3.13 or newer**. macOS ships 3.9, which is too old — installing with it
fails outright:

```
ERROR: Package 'sesame' requires a different Python: 3.9.6 not in '>=3.13'
```

Homebrew is the least painful way to get both.

**Install Homebrew** ([brew.sh](https://brew.sh)) if you don't have it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

On Apple Silicon it installs to `/opt/homebrew`, which is not on `PATH` by default. The installer
prints these two lines at the end — run them:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

**Then install pipx and a modern Python:**

```bash
brew install pipx python@3.13
pipx ensurepath
```

`ensurepath` adds `~/.local/bin` to your shell profile. Open a new terminal afterwards, or
`source ~/.zshrc`.

**Without Homebrew:** get Python 3.13+ from [python.org](https://www.python.org/downloads/macos/),
then use it to install pipx:

```bash
python3.13 -m pip install --user pipx
python3.13 -m pipx ensurepath
```

**If pipx picks the wrong Python** — the version error above — point it at the right one:

```bash
pipx install --python python3.13 git+https://github.com/SesameH/Sesame-GPS.git
```

**Already using [uv](https://docs.astral.sh/uv/)?** It does the same job and fetches a suitable
Python by itself:

```bash
uv tool install git+https://github.com/SesameH/Sesame-GPS.git
```

## Quick start

```bash
sesame --open
```

That's the whole thing. It starts the tunnel daemon if it isn't already running (asking for your
password once), serves the interface on <http://127.0.0.1:8765>, and opens your browser.

Then:

1. **重新掃描** (Rescan) → pick your device → **連線** (Connect)
2. Press **掛載 DDI** (Mount DDI) once — needed on first run and after every device reboot
3. Click anywhere on the map to put the device there

> The interface is currently in Traditional Chinese. English labels are given in brackets
> throughout this document.

### Why it needs your password

iOS 17+ requires a RemoteXPC tunnel, and creating the network interface for it needs root. Only
the tunnel daemon runs as root — `sesame` itself does not. The daemon is started detached, so it
**outlives** the app: restarting `sesame` does not tear down and rebuild the tunnel.

Pass `--no-tunneld` if you would rather run the daemon yourself:

```bash
sudo sesame daemon start     # foreground, Ctrl-C to stop
sesame --no-tunneld --open   # in another terminal
```

### Never being asked again

Install the tunnel daemon as a LaunchDaemon and it starts at boot:

```bash
sudo sesame daemon install     # install, runs at boot
sesame daemon status           # check
sudo sesame daemon uninstall   # remove
```

This writes `/Library/LaunchDaemons/com.sesame.tunneld.plist` and logs to
`/var/log/sesame-tunneld.log`. After this, `sesame` never touches `sudo`.

## Setting a location

There are three ways to place the device.

**Click the map.** In **點擊瞬移** (Click to teleport) mode, a click sends that point to the device.

**Type it.** The 位置 (Location) field takes coordinates or a place name:

| Input | Example |
| --- | --- |
| Decimal | `25.033964, 121.564468` — comma, semicolon or space |
| DMS | `25°02'02.3"N 121°33'52.1"E` — either order |
| Google Maps link | `https://www.google.com/maps/@25.033964,121.564468,17z`, `?q=…`, `?ll=…` |
| Place name | `Taipei 101` — looked up via Nominatim, then filled back in as coordinates |

Pressing Enter on **coordinates** sends them straight to the device. Pressing Enter on a **place
name** only looks it up and fills in the coordinates, leaving the send to you.

Four buttons, all of which accept coordinates or a place name:

- **定位到此** (Go here) — send to the device
- **只移動地圖** (Pan only) — move the map, leave the device alone
- **加到路線** (Add to route) — append to the route, for typing an exact path
- **帶入目前位置** (Use current) — fill the field with the device's current simulated position

**Draw a route.** See below.

## Routes

Switch to **畫路線** (Draw route) mode, then:

- **Click the map** to add a point
- **Drag a circle** to move it
- **Right-click a circle** to delete it
- **Click the line** to insert a waypoint at that spot

Every edit is live — there is no separate "finish drawing" step.

Set a speed and press **開始** (Start). Controls:

| Control | Behaviour |
| --- | --- |
| Speed | Adjustable **while running** — takes effect on the next tick. Presets: walk 5, jog 10, bike 20, drive 60 km/h |
| **循環** (Loop) | On reaching the end, continue from the start |
| **抖動** (Jitter) | Add 0–3 m of random offset so the track isn't a perfect line |
| Progress bar | Drag to seek |
| **暫停** / **停止** (Pause / Stop) | Pausing does not accumulate distance |

Movement is parameterised by **arc length**, not by vertex index: the device reports the speed you
asked for whether you drew two points or two thousand. See [How it works](#how-it-works).

## Device discovery

The device list comes from the tunnel daemon and is labelled by how each device was found:

| Label | Source | Requires |
| --- | --- | --- |
| `USB` | usbmuxd over the cable, or a USB CDC-NCM interface | Cable attached |
| `WiFi` | mobdev2 (Bonjour lookup of paired devices), a usbmux network device, or RemotePairing | Previously paired + same network |

**For Wi-Fi discovery, pair once over USB:**

```bash
sesame pair
```

This is not the same as "Trust This Computer". Wi-Fi discovery works by matching the Wi-Fi MAC a
device broadcasts over Bonjour against `WiFiMACAddress` in a pairing record under
`~/.pymobiledevice3`. usbmuxd keeps its own records elsewhere, so **a device can work perfectly
over the cable while Wi-Fi finds nothing at all** — there is simply nothing to match against. The
interface says so when that is the case rather than leaving you to guess.

After pairing, the daemon picks the device up on the same Wi-Fi with no cable. A locked device may
not be found; unlock it and rescan.

USB and Wi-Fi monitoring are both **on by default**; no flags needed.

When one device is reachable over both, the list keeps the USB entry — it survives the phone
sleeping or roaming between access points.

Devices you have seen before are remembered in `~/.sesame/devices.json` and stay in the list while
offline (marked 離線, not selectable), so a phone stays recognisable by name.

## Bundling as a double-clickable app

```bash
sesame app build          # creates ~/Applications/Sesame.app
```

Launch it from Launchpad or Finder. With no terminal to prompt on, it asks for your password
through the native macOS authorisation dialog. Startup output goes to `~/Library/Logs/sesame.log`.

If an instance is already running it won't start a second one — it just points your browser at the
running one. If the port is taken by something else you get a dialog naming what holds it, not a
silent death in a log file.

**Closing the tab quits the app.** Twenty seconds after the last browser disconnects the server
shuts down, which also hands GPS back to the device — a reload or a moment on another tab is not
long enough to trigger it. Running `sesame` from a terminal does not do this; pass
`--quit-on-close` if you want it there too.

The `.app` is a **launcher, not a frozen bundle**: it points at the `sesame` executable of
whichever environment you built it from, so re-run `app build` if that moves. This keeps the build
instant and avoids shipping pymobiledevice3's native dependencies, which would need signing and
notarisation to run elsewhere anyway. `--dest` changes where it goes, `--port` which port it uses.

The icon comes from `sesame/assets/icon.png` (`--icon` for another file; animated GIFs work, first frame
is used). The same image is served as the web favicon.

Icon processing:

- **Nearest-neighbour scaling** to all ten sizes before handing off to `iconutil`. Pixel art run
  through a smooth filter (`sips` included) turns to mush.
- **macOS rounded corners** — a superellipse (n=5 squircle, not a plain rounded rect), inset to
  824/1024 of the canvas per Apple's icon grid so it sits at the same visual size as everything
  else in the Dock. The corner mask is the one antialiased part of the pipeline; a hard-edged
  superellipse looks ragged at small sizes. `--square` skips it.

## Basemaps

Switch in the top right. All three need **no API key**:

| Layer | Source |
| --- | --- |
| Dark (default) | Esri World Dark Gray Canvas + reference layer |
| Streets | OpenStreetMap |
| Satellite | Esri World Imagery + boundaries and places |

CARTO and Stadia are deliberately avoided. CARTO now stamps "API KEY REQUIRED" across every tile
**and still answers HTTP 200**, so a missing key produces no error at all — just a mysteriously
dark map.

## HTTP API

The interface is a client of this; you can drive it yourself.

| Method | Path | Body / notes |
| --- | --- | --- |
| GET | `/api/devices` | Devices the tunnel daemon can see |
| GET | `/api/status` | Current session and route state |
| POST | `/api/connect` | `{"udid": "..."}` |
| POST | `/api/disconnect` | |
| POST | `/api/mount` | Mount the Developer Disk Image |
| POST | `/api/location` | `{"lat": …, "lon": …}` — single point |
| POST | `/api/clear` | Hand GPS back to the device |
| POST | `/api/route/start` | `{"points": [[lat, lon], …], "speed_kmh": 5, "loop": false, "jitter_m": 0}` |
| POST | `/api/route/pause` `/resume` `/stop` | |
| POST | `/api/route/speed` | `{"speed_kmh": …}` — valid mid-run |
| POST | `/api/route/seek` | `{"fraction": 0.0–1.0}` |
| WS | `/ws` | State pushed on every write (~10 Hz) |

## How it works

Two decisions carry the design.

**One connection for the whole session.** The tunnel, `DvtProvider` and `LocationSimulation` are
each opened once on connect. Every subsequent update is a single RPC on that same live channel —
nothing is rebuilt per coordinate. In front of it sits a **single-slot mailbox**: a new coordinate
overwrites one that hasn't been written yet, so a fast producer can never outrun the device, it
just skips intermediate points. Only a genuine channel failure triggers a reconnect, and that
swaps the DVT channel in place with backoff and replays the last coordinate — the session itself
survives.

**Movement is integrated over arc length.** Each tick does `distance += speed × dt`, then resolves
that distance against the polyline (binary search over a cumulative-length table plus great-circle
interpolation). Vertex spacing therefore has no effect on the reported speed, and changing the
speed mid-run takes effect on the next tick without restarting anything.

Write throttling lives in `engine.MIN_WRITE_INTERVAL` (0.1 s); the route ticker in
`RouteRunner(tick_hz=10)`.

## Project layout

| File | Responsibility |
| --- | --- |
| [`sesame/geo.py`](sesame/geo.py) | Great-circle distance, bearing, interpolation; `Path` carries a cumulative-length table and `Path.at(distance)` is what makes constant speed work |
| [`sesame/engine.py`](sesame/engine.py) | `DeviceSession` (persistent channel, single-slot mailbox, backoff reconnect) and `RouteRunner` (arc-length ticker) |
| [`sesame/server.py`](sesame/server.py) | FastAPI REST endpoints and WebSocket broadcast |
| [`sesame/static/index.html`](sesame/static/index.html) | Leaflet interface with hand-rolled vertex editing |
| [`sesame/__main__.py`](sesame/__main__.py) | CLI, tunnel daemon management, `.app` builder |

## Development

```bash
git clone https://github.com/SesameH/Sesame-GPS.git
cd Sesame-GPS
uv sync
uv run sesame --open
```

Every command in this document works the same way with `uv run` in front of it.

```bash
uv run pytest        # no device needed
uv run ruff check .
```

## Tests

```bash
uv run pytest
```

No device required. Route tests drive a fake session that records what would have been written,
and check realised ground speed, that pausing doesn't accumulate distance, mid-run speed changes,
loop wrapping, and end-of-route behaviour.

## Troubleshooting

**`cannot reach tunneld`** — the daemon isn't running, or wasn't started with `sudo`.

**No devices found** — check the cable, "Trust This Computer" on the device, and Settings →
Privacy & Security → Developer Mode.

**Found over USB but never over Wi-Fi** — you are missing a pairing record. Plug in and run
`sesame pair`, then unplug. See [Device discovery](#device-discovery) for why the two paths differ.

**Nothing on Wi-Fi even after pairing** — unlock the phone and rescan. A freshly started tunnel
daemon also needs a few seconds; the interface retries for about thirty.

**Mounting the DDI fails** — the first mount downloads a personalized image from Apple, so it needs
internet access. On iOS 17+ this must be redone after every device reboot.

**The reconnect counter keeps climbing** — try a different cable or port. Dropped tunnels are
recovered automatically, but constant drops mean a physical connection problem.

## A note on exposure

The interface has **no authentication of any kind**. It binds to `127.0.0.1` by default, which
keeps it on your machine. `--host 0.0.0.0` makes it reachable from your network — and to anyone
else on that network, with no password. Prefer an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 your-mac
```

## License

MIT
