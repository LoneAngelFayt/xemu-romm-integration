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
- Controller support via the selkies joystick interposer (gamepad auto-configured on port 1)
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
| `STATE_FILE_MAX_BYTES` | `268435456` | Size ceiling for a state archive in either direction |
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

Validates `rom_path` is within `ROM_ROOT`, then starts a background thread that:
1. Polls QMP until xemu is ready (up to `QMP_BOOT_TIMEOUT` seconds)
2. Inserts the disc via `blockdev-change-medium`
3. Sends `system_reset` and waits for the `RESET` event confirmation (3 retries)

Returns `200 {"status": "loading"}` immediately. Poll `/status` to confirm `active: true`.

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

A QMP snapshot cannot be exported on its own, so the portable artifact is the whole hard disk image, zipped. After a save, RomM calls `GET /state-file?slot=N`, which pauses the vCPUs (so the qcow2 is not read mid-write), zips the image, resumes, and returns it as `<rom>.xNN`. The archive is small in practice — a fresh image with several snapshots compresses to well under a megabyte.

To resume, RomM pushes the archive back with `PUT /state-file?filename=<rom>.xNN` while xemu is stopped (the call returns `409` otherwise), then launches with `{"rom_path": "...", "load_slot": N}`. The broker inserts the disc first and loads the snapshot after, so the restored machine is already running that disc.

The slot number is a container-side QMP handle only. RomM stores states as history, pruned to its configured limit, not in fixed slots.

## Architecture

The broker owns the xemu process. The desktop autostart is neutered at startup so no gameless xemu is running at the dashboard; the broker spawns one with a QMP socket when a ROM is launched and kills it when the session ends.

```
Startup (init.sh)
  └── Write broker-managed autostart (labwc + openbox): no boot-time xemu
  └── Seed xemu.toml: port1_driver = 'usb-xbox-gamepad'
  └── Pin renderer = 'VULKAN' (AMD GPUs): xemu's OpenGL path hangs the GPU
  └── Copy the stock Xbox HDD image to /config/xemu and repoint hdd_path
  └── chown xemu config dir to abc

Broker (broker.py, port 8000)
  └── POST /launch     → spawn xemu -qmp … → blockdev-change-medium + system_reset
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
