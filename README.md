# xemu-romm-integration-mod

A [linuxserver Docker mod](https://docs.linuxserver.io/general/container-customization/#docker-mods) for [linuxserver/xemu](https://docs.linuxserver.io/images/docker-xemu/) that adds an HTTP broker for [RomM](https://github.com/rommapp/romm) streaming integration.

Launch original Xbox games from the RomM web UI with save states, controller input, and volume control. The broker owns the xemu process: it spawns xemu with a QMP socket when a ROM is launched and kills it when the session ends, so no gameless instance is left burning CPU at the dashboard.

## Prerequisites

xemu requires BIOS files and an Xbox HDD image before it will run games. Configure these in the xemu settings UI before launching anything via RomM. Open the container's web interface and set:

- **MCPX boot ROM** — `mcpx_1.0.bin`
- **Xbox BIOS** — e.g. `complex_4627v1.03.bin`
- **Xbox HDD image** — `xbox_hdd.qcow2` (xemu can generate a blank one)

Save states are stored as named snapshots inside the HDD image. The HDD image must be configured before save/load state calls will work.

## Features

- Launch Xbox ROMs on demand from RomM (XISO `.iso` format)
- Stop xemu when a session ends, so no gameless instance burns CPU at the dashboard
- Save state support — 9 user slots + 1 autosave slot (slot 10), stored inside the Xbox HDD image
- Volume and mute control via PulseAudio
- Reliable sound — creates the PulseAudio sinks selkies captures from before selkies starts, closing a startup race in the base image that silences the stream at random
- Controller support via the selkies joystick interposer (gamepad auto-configured on port 1)
- Fills the stream — seeds `fullscreen_on_startup` so xemu's window covers the streamed canvas instead of sitting in a corner of it
- AMD GPU support — pins the Vulkan renderer in `xemu.toml`, since xemu's OpenGL path hangs the GPU here
- Save states importable into the RomM library, and resumable on any container
- Per-container Xbox hard disk image, so players never share save data

## Why there is no in-game save sync

PCSX2 and Dolphin expose in-game saves as files the broker can read directly.
xemu keeps them in a FATX filesystem inside the hard disk qcow2, which would
need both a qcow2 reader and a FATX reader written from scratch to reach, and
the broker is stdlib only. Save states carry the whole disk image instead, so
in-game saves travel inside them.

One consequence: a save state restores the entire console, including every
title's saves as they stood when it was captured. Restoring an old state for one
game rolls back in-game saves for other games too.

## Roadmap

### Per-user hard disk, carried like a memory card

The hard disk is currently a fixture of the container: one image, shared by
whoever streams next, holding every title's in-game saves for every user on the
platform. That is the wrong owner. It makes one player's progress visible to the
next, it makes the image grow without bound as users accumulate, and it is why a
state archive has to carry the whole console.

The idea is to make the image a per-user asset RomM stores and hands back at
launch, the way a memory card follows its owner rather than living in the
console. The broker would receive the player's image with the launch and return
it when the session ends, so the container keeps nothing between sessions.

Open questions before this is worth planning: how it interacts with the save
states above (both are the same qcow2 today), what RomM's asset model can
already express, whether the handoff can be made cheap enough to sit in the
launch path, and what happens when the same user streams from two places at
once.

## State thumbnails

Frames come from pixelflux, which is the compositor when selkies runs in
Wayland mode. `init.sh` enables its Computer Use HTTP server by writing
`PIXELFLUX_CU` into the container environment before selkies starts, so
thumbnails need no configuration from whoever installs the mod. At save time the
broker posts `{"action": "screenshot"}` to it and stores the base64 PNG that
comes back beside the disk image, where `GET /state-screenshot` serves it.

What comes back is the composited output — the picture the player is actually
looking at — which is why this works where `screendump` would not.

**The port is not published, and must stay that way.** The Computer Use API
carries no credential and injects keyboard and mouse as well as capturing
frames, so anything that can reach it drives the desktop. Unpublished it is
reachable only from inside the container, which is where the broker runs. Set
`PIXELFLUX_CU` to move it off the default `8085`, or to `0` to leave the server
off and states without thumbnails.

This needs a base image new enough to have the feature. pixelflux 1.6.4, which
ships in `v0.8.134-ls76`, ignores `PIXELFLUX_CU`; the broker then logs a refused
connection, stores no frame, and `GET /state-screenshot` returns `404` as before.

Everything that could capture a frame without pixelflux is closed:

| Approach | Why it fails |
|---|---|
| QMP `screendump` | Declared `'if': 'CONFIG_PIXMAN'` in `qapi/ui.json`, and xemu ships without pixman, so the command is not registered. `query-commands` lists 228 commands and no capture command among them |
| Rebuilding xemu with pixman | `option('pixman')` is `auto`, not disabled, so a rebuild would register the command — but xemu [#774](https://github.com/xemu-project/xemu/issues/774) reports it dumps a stale Xbox logo. The nv2a renderer draws to the host GL surface and never populates the `DisplaySurface` that `screendump` reads, which is also why pixman is absent to begin with |
| `xwd` against X11 | xemu creates no X window. Under Xwayland only an 8192x8192 unmapped virtual root exists, and `X_GetImage` on it fails `BadMatch` |
| `grim` against Wayland | pixelflux is the compositor and implements no `wlr-screencopy-unstable-v1`, so no generic Wayland client can copy the screen |
| A second `pixelflux.ScreenCapture` | It starts its own compositor rather than attaching to the running one, so it would capture a blank framebuffer, not the game |

The `ImageFormat` enum is *not* gated on pixman, so `screendump` and its `ppm`
and `png` tokens appear in the binary's strings whether or not the command
exists. Checking for them is not a valid test for support; ask `query-commands`.

**Why not capture in the browser.** RomM decodes and displays every frame
client-side, so the player view looks like the obvious place to grab one, and it
is how EmulatorJS states get their images — `gameManager.screenshot()` reads its
own canvas, and RomM's state upload already carries an optional screenshot file.
The streaming player cannot do the same: it embeds the container's selkies UI in
a cross-origin iframe, and a parent document cannot read pixels out of one. It
would only work where RomM and the emulator container happen to share an origin,
which is not the normal deployment.

## Usage

```yaml
services:
  xemu:
    image: lscr.io/linuxserver/xemu:latest
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
      - DOCKER_MODS=ghcr.io/YOUR_USERNAME/xemu-romm-integration-mod:latest
      - BROKER_PORT=8000
      - BROKER_SECRET=your-secret-here
      - ROM_ROOT=/romm/library
    volumes:
      - ./config:/config
      - /path/to/romm/library:/romm/library:ro
    ports:
      - 3000:3000   # selkies WebRTC stream
      - 8000:8000   # broker API
    restart: unless-stopped
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BROKER_PORT` | `8000` | HTTP API port |
| `BROKER_SECRET` | unset | Shared secret — POST/DELETE require `X-Broker-Secret` header when set |
| `ROM_ROOT` | `/romm/library` | ROM path validation root; paths outside this are rejected |
| `QMP_SOCKET` | `/tmp/xemu-qmp.sock` | Path to the xemu QMP Unix socket |
| `QMP_TIMEOUT` | `2.0` | QMP connect/handshake timeout in seconds |
| `QMP_WAIT` | `10.0` | Max seconds to wait for a snapshot job or reset event to complete |
| `XEMU_CMD` | `/opt/xemu/AppRun` | Command the broker spawns to start xemu |
| `QMP_BOOT_TIMEOUT` | `60.0` | Max seconds to wait for xemu to become QMP-ready after `/launch` |
| `SETUP_TIMEOUT` | `900.0` | Seconds a `/setup` session stays up before the broker auto-stops idle xemu |
| `BROKER_LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `HDD_IMAGE` | `/config/xemu/xbox_hdd.qcow2` | Xbox hard disk image the broker reads and restores as a save state |
| `HDD_STOCK` | `/config/bios/Xbox Hard Disk Image/xbox_hdd.qcow2` | Stock image `init.sh` copies from when the container-local one is missing or unusable |
| `STATE_FILE_MAX_BYTES` | `2147483648` | Size ceiling for a state **archive** in either direction. Set to match the expanded ceiling below so that one is what binds: a zip of a qcow2 is never bigger than the qcow2, so this is a backstop rather than a limit real saves meet. A first save is the largest one — the image is at its fattest before the trim has an older snapshot to drop |
| `HDD_IMAGE_MAX_BYTES` | `2147483648` | Size ceiling for the **expanded** hard disk image. Separate from the archive limit because a qcow2 carrying a ~70MB VM state runs well past its own zipped size, and a qcow2 never shrinks when a snapshot is deleted |
| `STATE_TRIM` | `1` | Rebuild the image around the one snapshot being served so the other slots do not ship inside the archive. `0` serves the whole image, which is also where any failed or refused rebuild falls back to |
| `PIXELFLUX_CU` | `8085` | Container-internal port of the pixelflux Computer Use server that state thumbnails are captured from. `0` disables it. **Never publish this port** — see [State thumbnails](#state-thumbnails) |
| `STATE_SHOT_TIMEOUT` | `10.0` | Seconds to wait for a captured frame before giving up and saving without one |
| `STATE_GET_WAIT` | `30.0` | Max seconds `GET /state-file` waits for an in-flight save to finish |
| `STOP_WAIT` | `5.0` | Max seconds `DELETE /launch` waits for an in-flight `/state-file` transfer or snapshot job before stopping xemu anyway |
| `BROKER_REQUEST_TIMEOUT` | `60.0` | Per-socket HTTP request timeout; a client that stalls mid-request is dropped rather than holding a handler thread |

## Broker API

Every endpoint requires `X-Broker-Secret: <secret>` when `BROKER_SECRET` is configured. `GET /health` is the only exception, so container healthchecks still work.

### Read

| Endpoint | Method | Response |
|---|---|---|
| `/health` | GET | `{"status": "ok"}` |
| `/status` | GET | Session state — see below |
| `/state-file?slot=N` | GET | The zipped Xbox hard disk image holding slot N's capture, named by `X-State-Filename` |
| `/state-screenshot?slot=N` | GET | The PNG frame captured when slot N was saved, or `404` if none was — see [State thumbnails](#state-thumbnails) |

**`GET /status` response:**
```json
{
  "xemu_running": true,
  "active": true,
  "setup": false,
  "rom_path": "/romm/library/roms/xbox/Fable.xiso.iso",
  "rom_name": "Fable",
  "started_at": "2026-04-25T11:50:00Z",
  "launch_error": null,
  "resume_error": null
}
```
`active` is true only when xemu is reachable via QMP **and** a ROM has been loaded. `setup` is true only when xemu is up for a `/setup` configuration session with no ROM loaded, so `active` and `setup` are never both true.

`launch_error` is `null` on success; after a failed `/launch` it holds the reason (QMP never came up, or the ROM could not be loaded) so the frontend can show why the game never started. When it is set there is no session — `active` is false.

`resume_error` is the opposite case: the ROM did launch, but the `load_slot` it was asked to resume from held no usable state, so the game booted fresh. `active` stays true and `launch_error` stays `null`, so this is the only field telling the frontend the player is not where they left off. Both clear at the start of the next launch and when a session ends via `DELETE /launch` or `/save-and-exit`.

### Write

| Endpoint | Method | Body | Description |
|---|---|---|---|
| `/launch` | POST | `{"rom_path": "...", "load_slot": 1–10}` | Inject a ROM and boot the console, optionally resuming from a slot |
| `/setup` | POST | — | Boot xemu with no disc at the dashboard so it can be configured, auto-stopping after `SETUP_TIMEOUT` |
| `/launch` | DELETE | — | End the active game or setup session and stop xemu |
| `/state-file?filename=<name>.xNN` | PUT | Zipped hard disk image | Restore a state pulled from RomM. Rejected while xemu is running |
| `/cleanup` | POST | — | Restart selkies to flush stale gamepad sockets |
| `/save-and-exit` | POST | `{"slot": 0–10, "wait": true\|false}` | Save (slot defaults to 10, the autosave slot) and stop xemu |
| `/save-state` | POST | `{"slot": 1–10}` | Save state to the given slot |
| `/load-state` | POST | `{"slot": 1–10}` | Load state from the given slot |
| `/volume` | POST | `{"level": 0–100}` | Set PulseAudio sink volume |
| `/mute` | POST | `{"mute": true\|false}` or `{}` | Set or toggle mute |

#### `/launch` (POST)

Validates `rom_path` is within `ROM_ROOT` and resolves it to a disc image (see
[ROM path resolution](#rom-path-resolution)), then starts a background thread that:
1. Spawns xemu with the disc already in the drive (`-dvd_path`) if none is running, so the game boots from power-on
2. Polls QMP until xemu is ready (up to `QMP_BOOT_TIMEOUT` seconds)
3. Only when reusing an already-booted xemu: inserts the disc via `blockdev-change-medium`, then sends `system_reset` and waits for the `RESET` event confirmation (3 retries)

A cold start must never be reset. QMP answers about a second after the process starts, while the guest is still inside the MCPX bootrom, and a reset landing there wedges the machine — it stays `running` and burns a full core, but never draws a frame or plays a sample.

Returns `200 {"status": "loading"}` immediately. Poll `/status` to confirm `active: true`.

##### ROM path resolution

`rom_path` may be either a file or a **directory**, for libraries laid out one
game per folder (`roms/xbox/Fable/Fable.xiso.iso`). RomM addresses such a game
by its folder, because `Rom.full_path` is `fs_path/fs_name` and for a
multi-file ROM `fs_name` is the directory, so the broker looks inside for the
disc image: the folder itself first, then one level down for the per-disc
subfolders some sets use. Only XISO images (`.iso`, including the `.xiso.iso`
double extension) are considered, ranked by name so a multi-disc set boots
disc 1. Dot-files are skipped, and a symlink pointing outside `ROM_ROOT` is
never chosen. The resolved file is what `/status` and the response body report.

A directory with no disc image inside returns `422` with the accepted
extensions in an `extensions` field, which is a different message from the
`422` for a path that does not exist at all.

#### `/setup` (POST)

Boots xemu with no disc so its own menus (BIOS paths, video, input) can be reached over the stream, since the broker otherwise only runs xemu while a ROM is loaded. Starts a background thread that spawns xemu and waits for QMP, then arms a watchdog that stops xemu after `SETUP_TIMEOUT` seconds so it never idles indefinitely (a gameless xemu busy-loops the CPU).

Returns `409` if a game session is active or a launch is in progress, and `200 {"status": "setup", "already": true}` if setup is already running. Otherwise returns `200 {"status": "starting setup"}` immediately; poll `/status` for `setup: true`. A real `POST /launch` supersedes an in-progress setup, and `DELETE /launch` ends it early.

#### `/launch` (DELETE)

Kills the xemu process and clears broker session state, ending an active game **or** a `/setup` session. xemu is stopped rather than returned to the dashboard because a gameless instance busy-loops several CPU cores under software rendering.

An in-flight `/state-file` transfer or snapshot job gets up to `STOP_WAIT` seconds to finish first — the transfer holds the guest paused mid-read, and a save is a snapshot job a `SIGTERM` would cut in half. The stop still wins once that window runs out, since it is the only way out of a hung session.

#### `/save-and-exit` (POST)

Body: `{"slot": 0–10, "wait": true|false}`. Both are optional — `slot` defaults to 10 (autosave) and `0` is a legacy value remapped to 10; `wait` defaults to `true`.

Saves to the slot via QMP, then kills xemu. With `wait: true` the save runs synchronously and the response carries `{"status": "ok", "saved": <bool>, "slot": N}`; with `wait: false` it is queued on a background thread and the response is `{"status": "queued", "slot": N}`. If the save fails, xemu is stopped anyway and a warning is logged.

#### `/save-state` (POST)

Saves to the given slot (1–10) using the QMP `snapshot-save` job API. If a snapshot for that slot already exists, it is deleted first. The API call blocks until the job completes (up to `QMP_WAIT` seconds).

#### `/load-state` (POST)

Loads the given slot (1–10) using the QMP `snapshot-load` job API. Returns `503` if the slot does not exist or the load fails.

#### `/volume` (POST)

Sets the PulseAudio default sink volume. Returns `{"status": "ok", "level": <N>}`.

#### `/mute` (POST)

Sets or toggles mute on the PulseAudio default sink. Omit `mute` for toggle. Returns `{"status": "ok", "mute": true|false}`.

## Save States

Save states are QMP snapshots stored inside the Xbox HDD image (`xbox_hdd.qcow2`). The broker uses xemu's `snapshot-save` / `snapshot-load` / `snapshot-delete` job API — calls block until the job completes so responses reliably reflect success or failure.

Snapshot names in the HDD image: `broker-slot-1` through `broker-slot-10`.

Slot 10 is reserved for the autosave triggered by `/save-and-exit`. Slots 1–9 are user-controlled. All 10 slots are accessible via `/save-state` and `/load-state`.

### Import and resume

A QMP snapshot cannot be exported on its own, so the portable artifact is the hard disk image, zipped. After a save, RomM calls `GET /state-file?slot=N`, which pauses the vCPUs (so the qcow2 is not read mid-write), zips the image, resumes, and returns it as `<rom>.xNN`.

Because every slot lives in that one image, shipping it whole would put slots 1–4 inside the archive for slot 5, and archives would grow with every save. So the image is first rebuilt around the snapshot being served: the active disk and that one snapshot are copied into a fresh qcow2 and the rest is left behind, along with any clusters an interrupted snapshot job leaked. Archive size then depends on the state being saved rather than on how many saves came before it — on a real image with two slots this took the archive from 83MB to 53MB, and the second number no longer climbs.

The rebuilt image is checked before it is used: the guest-visible bytes of both surviving mappings are compared against the original cluster by cluster, and the refcounts are recomputed from the tables. Anything short of an exact match, an image the rebuild does not understand (compressed clusters, a backing file, encryption), or `STATE_TRIM=0` all fall back to serving the untouched image, so this can only ever cost archive size, never a save.

A restored state therefore contains only its own snapshot. Loading slot 5 no longer brings slots 1–4 back with it — RomM is the store of record for state history, not the image.

To resume, RomM pushes the archive back with `PUT /state-file?filename=<rom>.xNN` while xemu is stopped (the call returns `409` otherwise), then launches with `{"rom_path": "...", "load_slot": N}`. The broker inserts the disc first and loads the snapshot after, so the restored machine is already running that disc.

The slot number is a container-side QMP handle only. RomM stores states as history, pruned to its configured limit, not in fixed slots.

## Architecture

The broker owns the xemu process. The desktop autostart is neutered at startup so no gameless xemu is running at the dashboard; the broker spawns one with a QMP socket when a ROM is launched and kills it when the session ends.

```
Startup (init-xemu-audio)
  └── Wait for PulseAudio to answer, then create the `output`/`input` null sinks
      before selkies starts: the base image's own setup can lose that race

Startup (init.sh)
  └── Write broker-managed autostart (labwc + openbox): no boot-time xemu
  └── Seed xemu.toml: port1_driver = 'usb-xbox-gamepad'
  └── Seed fullscreen_on_startup = true: else xemu draws in a corner of the stream
  └── Pin renderer = 'VULKAN' (AMD GPUs): xemu's OpenGL path hangs the GPU
  └── Copy the stock Xbox HDD image to /config/xemu and repoint hdd_path
  └── chown xemu config dir to abc

Broker (broker.py, port 8000)
  └── POST /launch     → spawn xemu -dvd_path <rom> -qmp … (cold start boots the disc, no reset)
  │                      or blockdev-change-medium + system_reset when reusing a booted xemu
  │                      (optional load_slot resumes a snapshot after the disc is in)
  └── POST /setup      → spawn xemu -qmp … at the dashboard (no disc), auto-stop after SETUP_TIMEOUT
  └── DELETE /launch   → kill xemu (ends a game or setup session)
  └── GET  /state-file → QMP stop → zip xbox_hdd.qcow2 → cont
  └── PUT  /state-file → restore a zipped image (only while xemu is stopped)
  └── POST /save-state → snapshot-delete (stale) + snapshot-save (async job)
  └── POST /load-state → snapshot-load (async job)
  └── POST /save-and-exit → snapshot-save slot 10 + kill xemu
  └── POST /cleanup    → restart selkies to flush stale gamepad sockets
  └── POST /volume     → pactl set-sink-volume
  └── POST /mute       → pactl set-sink-mute

xemu (spawned per session)
  └── Xbox HDD + BIOS loaded, QMP socket listening on /tmp/xemu-qmp.sock

RomM ←→ selkies WebRTC ←→ browser
```

## Troubleshooting

**Game doesn't load after `/launch`**
Poll `/status` — if `active` is still false after 60s, check container logs for QMP errors. xemu may not have finished booting.

**Save state fails with "Snapshot already exists"**
This should not happen with the current broker — it deletes the old snapshot before saving. If it does, check logs for the `snapshot-delete` step.

**Save state times out**
Large game states can take more than the default 10s. Set `QMP_WAIT=30` (or higher) in your environment.

**No controllers visible in xemu**
The selkies joystick interposer requires the streaming session to be active before xemu starts. Connect via the RomM player first, then launch a game.

**xemu shows black screen or won't start**
xemu only runs while a game is loaded. If a gameless instance is burning CPU at the dashboard, check `/config/.config/labwc/autostart` (or the openbox one) — it should contain the `broker-managed` marker and no `AppRun` line. Remove the file and restart the container to let `init.sh` recreate it.

**Settings not saved after restart**
The xemu config directory must be writable by the `abc` user. `init.sh` runs `chown -R abc:abc` on startup, but if the `/config` volume has permission issues this may fail — check container logs for the ownership line.
