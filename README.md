# sesame

Simulate your iPhone's GPS location from a map in your browser. Click to teleport, or draw a
route and walk it at a real, constant speed.

Built on [pymobiledevice3](https://github.com/doronz88/pymobiledevice3). macOS only.

## Requirements

- macOS
- An iPhone or iPad running **iOS 17 or later**, with Developer Mode enabled
  (Settings → Privacy & Security → Developer Mode)
- Python 3.13 or newer (macOS ships 3.9 — see below)

The device must be reachable from this Mac — over USB, or over Wi-Fi once it has been paired.

## Install

```bash
pipx install git+https://github.com/SesameH/Sesame-GPS.git
```

`sesame` is then on your `PATH`. Upgrade with `pipx upgrade sesame`, remove it with
`pipx uninstall sesame`.

### If you don't have pipx

pipx installs applications into their own isolated environment. You also need Python 3.13 or
newer — macOS ships 3.9, which is too old. Homebrew gets you both:

```bash
brew install pipx python@3.13
pipx ensurepath
```

No Homebrew? Install it first ([brew.sh](https://brew.sh)):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

On Apple Silicon, run the two lines it prints at the end to put `brew` on your `PATH`.

Prefer not to use Homebrew? Get Python 3.13+ from
[python.org](https://www.python.org/downloads/macos/), then:

```bash
python3.13 -m pip install --user pipx
python3.13 -m pipx ensurepath
```

`ensurepath` edits your shell profile, so open a new terminal afterwards.

If pipx picks the wrong Python, point it at the right one:

```bash
pipx install --python python3.13 git+https://github.com/SesameH/Sesame-GPS.git
```

Already use [uv](https://docs.astral.sh/uv/)? `uv tool install` does the same job and fetches a
suitable Python itself:

```bash
uv tool install git+https://github.com/SesameH/Sesame-GPS.git
```

## Quick start

```bash
sesame --open
```

It starts the tunnel daemon if it isn't already running (asking for your password once), serves
the interface on <http://127.0.0.1:8765>, and opens your browser.

1. **Rescan** → pick your device → **Connect**
2. Press **Mount DDI** once — needed on first run and after every device reboot
3. Click anywhere on the map to put the device there

The interface is in English; the button in the top-left corner switches it to Traditional Chinese
and remembers the choice.

### Not being asked for a password every time

Install the tunnel daemon so it starts at boot:

```bash
sudo sesame daemon install     # install
sesame daemon status           # check
sudo sesame daemon uninstall   # remove
```

After this, `sesame` never touches `sudo`.

To run the daemon yourself instead, pass `--no-tunneld`:

```bash
sudo sesame daemon start     # foreground, Ctrl-C to stop
sesame --no-tunneld --open   # in another terminal
```

## Setting a location

**Click the map.** In **Click to teleport** mode, a click sends that point to the device.

**Type it.** The **Location** field takes coordinates or a place name:

| Input | Example |
| --- | --- |
| Decimal | `37.3382, -121.8863` — comma, semicolon or space |
| DMS | `37°20'17.5"N 121°53'10.7"W` — either order |
| Google Maps link | `https://www.google.com/maps/@37.3382,-121.8863,17z`, `?q=…`, `?ll=…` |
| Place name | `San Jose City Hall` |

Enter on **coordinates** sends them straight to the device. Enter on a **place name** looks it up
and fills in the coordinates, leaving the send to you.

The four buttons all accept coordinates or a place name:

- **Go here** — send to the device
- **Pan only** — move the map, leave the device alone
- **Add to route** — append to the route, for typing an exact path
- **Use current** — fill the field with the device's current simulated position

The map opens over San Jose.

## Routes

Switch to **Draw route** mode, then:

- **Click the map** to add a point
- **Drag a circle** to move it
- **Right-click a circle** to delete it
- **Click the line** to insert a waypoint

Every edit is live — there is no separate "finish drawing" step.

Set a speed and press **Start**:

| Control | Behaviour |
| --- | --- |
| Speed | Adjustable **while running**. Presets: walk 5, jog 10, bike 20, drive 60 km/h |
| **Loop** | On reaching the end, continue from the start |
| **Jitter** | Add 0–3 m of random offset so the track isn't a perfect line |
| Progress bar | Drag to seek |
| **Pause** / **Stop** | Pausing does not accumulate distance |

The device reports the speed you asked for whether you drew two points or two thousand.

## Device discovery

Devices are labelled by how they were found:

| Label | Requires |
| --- | --- |
| `USB` | Cable attached |
| `WiFi` | Paired once over USB, and on the same network |

**For Wi-Fi, pair once over USB** — press **Pair over USB** in the interface, or:

```bash
sesame pair
```

This is not the same as "Trust This Computer". Without it the device works perfectly over the
cable and is never found over Wi-Fi. Afterwards you can unplug.

A locked phone may not be found; unlock it and rescan. Devices you have seen before stay in the
list while offline, marked as such.

### When nothing shows up

```bash
sesame doctor
```

Every cause looks the same from the interface — an empty device list — so this says which one it
is and what to do about it.

### What survives a restart

| Restarting | Still works? |
| --- | --- |
| Closing the app | Yes |
| **Rebooting the Mac** | Only if you installed the LaunchDaemon; otherwise you are asked for a password again |
| **Rebooting the phone** | Mount the DDI again |
| The phone changing its Wi-Fi address | Run `sesame pair` again |

## A double-clickable app

```bash
sesame app build          # creates ~/Applications/Sesame.app
```

Launch it from Launchpad or Finder. It asks for your password through the macOS dialog, and logs
to `~/Library/Logs/sesame.log`.

**Closing the tab quits the app.** Twenty seconds after the last browser disconnects the server
shuts down and hands GPS back to the device; a reload is not long enough to trigger it. Running
`sesame` from a terminal does not do this unless you pass `--quit-on-close`.

Re-run `app build` after upgrading or moving the project. `--dest` changes where it goes,
`--port` which port it uses, `--icon` the artwork (`sesame/assets/icon.png` by default), and
`--square` skips the macOS rounded corners.

## Basemaps

Switch in the top right. All three need no API key:

| Layer | Source |
| --- | --- |
| Dark (default) | Esri World Dark Gray Canvas |
| Streets | OpenStreetMap |
| Satellite | Esri World Imagery |

## Troubleshooting

**`cannot reach tunneld`** — the daemon isn't running, or wasn't started with `sudo`.

**No devices found** — check the cable, "Trust This Computer" on the device, and Settings →
Privacy & Security → Developer Mode. Then run `sesame doctor`.

**Found over USB but never over Wi-Fi** — you are missing a pairing record. Plug in and run
`sesame pair`, then unplug.

**Nothing on Wi-Fi even after pairing** — unlock the phone and rescan. A freshly started daemon
needs a few seconds; the interface retries for about thirty. If it persists, restart the daemon:

```bash
sudo sesame daemon restart
```

**Mounting the DDI fails** — the first mount downloads an image from Apple, so it needs internet
access. On iOS 17+ this must be redone after every device reboot.

**The reconnect counter keeps climbing** — try a different cable or port.

## A note on exposure

The interface has **no authentication of any kind**. It binds to `127.0.0.1` by default, which
keeps it on your machine. `--host 0.0.0.0` makes it reachable from your network — and to anyone
else on that network, with no password. Prefer an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 your-mac
```

## Development

```bash
git clone https://github.com/SesameH/Sesame-GPS.git
cd Sesame-GPS
uv sync
uv run sesame --open   # every command works the same with `uv run` in front
uv run pytest          # no device needed
```

## License

MIT
