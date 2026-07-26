# xemu-romm-integration-mod

A [linuxserver Docker mod](https://docs.linuxserver.io/general/container-customization/#docker-mods) for [linuxserver/xemu](https://docs.linuxserver.io/images/docker-xemu/) that adds an HTTP broker for [RomM](https://github.com/rommapp/romm) streaming integration.

Launch original Xbox games from the RomM web UI with save states, controller input, and volume control.

The broker owns the xemu process. It spawns xemu with a QMP socket when a ROM is launched and kills it when the session ends, so there is never a gameless instance sitting at the dashboard. That matters more than it sounds: xemu is QEMU-based and has no frame limiter with nothing loaded, so an idle instance pegs a CPU or GPU for as long as the container is up.

## Features

- Launch Xbox ROMs on demand from RomM (XISO `.iso`)
- Save states are exportable into the RomM library and resumable on any container
- A hard disk image per game, swapped in at launch, so one game's writes never inflate another's saves
- Thumbnails captured for each state
- Volume and mute control via PulseAudio
- Controller support through the selkies joystick interposer, gamepad auto-configured on port 1
- Sound that works, by creating the PulseAudio sinks selkies captures from before selkies starts and closing a race in the base image that silences the stream at random
- A window that fills the stream, by seeding `fullscreen_on_startup`
- Vulkan pinned on AMD hosts, where xemu's OpenGL path hangs the GPU. NVIDIA keeps xemu's default

## Before you start

xemu needs BIOS files and a hard disk image before it will run anything. You will need to provide these on your own. Open the container's web interface and set:

- **MCPX boot ROM** — `mcpx_1.0.bin`
- **Xbox BIOS** — e.g. `complex_4627v1.03.bin`
- **Xbox HDD image** — `xbox_hdd.qcow2` (xemu can generate a blank one)

Since the broker only runs xemu while a game is loaded, `POST /setup` is how you reach those menus: it boots xemu at the dashboard and stops it again after `SETUP_TIMEOUT`.

## Usage

```yaml
services:
  xemu:
    image: lscr.io/linuxserver/xemu:latest
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
      - DOCKER_MODS=ghcr.io/loneangelfayt/xemu-romm-integration-mod:latest
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

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `BROKER_PORT` | `8000` | HTTP API port |
| `BROKER_SECRET` | unset | Shared secret. When set, every endpoint but `/health` requires an `X-Broker-Secret` header |
| `ROM_ROOT` | `/romm/library` | ROM path validation root; paths outside it are rejected |
| `XEMU_CMD` | `/opt/xemu/AppRun` | Command the broker spawns to start xemu |
| `BROKER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `QMP_SOCKET` | `/tmp/xemu-qmp.sock` | Path to the xemu QMP Unix socket |
| `QMP_TIMEOUT` | `2.0` | QMP connect and handshake timeout, seconds |
| `QMP_WAIT` | `10.0` | Longest wait for a snapshot job or reset event |
| `QMP_BOOT_TIMEOUT` | `60.0` | Longest wait for xemu to become QMP-ready after `/launch` |
| `SETUP_TIMEOUT` | `900.0` | How long a `/setup` session stays up before the broker stops it |
| `STOP_WAIT` | `5.0` | How long `DELETE /launch` waits for an in-flight transfer or snapshot job before stopping xemu anyway |
| `BROKER_REQUEST_TIMEOUT` | `60.0` | Per-socket HTTP timeout, so a client that stalls mid-request is dropped instead of holding a handler thread |
| `HDD_IMAGE` | `/config/xemu/xbox_hdd.qcow2` | The live hard disk image |
| `HDD_STOCK` | `/config/bios/Xbox Hard Disk Image/xbox_hdd.qcow2` | Blank image copied from when a game has no disk of its own |
| `HDD_STORE` | `/config/xemu/hdd` | Where each game's disk is kept while another game is playing |
| `STATE_TRIM` | `1` | Rebuild the image around the one snapshot being served so the other slots stay out of the archive. `0` serves the image whole |
| `STATE_FILE_MAX_BYTES` | `2147483648` | Ceiling on a state archive in either direction |
| `HDD_IMAGE_MAX_BYTES` | `2147483648` | Ceiling on the expanded image. Separate from the archive limit because a qcow2 carrying a VM state runs well past its own zipped size |
| `STATE_FILE_READ_TIMEOUT` | `300.0` | Deadline for a whole `PUT /state-file` body. Keep it above RomM's own upload timeout |
| `STATE_GET_WAIT` | `30.0` | How long `GET /state-file` waits for an in-flight save to finish |
| `PIXELFLUX_CU` | `8085` | Container-internal port of the frame capture server. `0` disables thumbnails. **Never publish this port** |
| `STATE_SHOT_TIMEOUT` | `10.0` | How long to wait for a frame before saving without one |

## Broker API

Every endpoint requires `X-Broker-Secret: <secret>` when `BROKER_SECRET` is set. `GET /health` is the exception, so container healthchecks keep working.

### Read

| Endpoint | Response |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `GET /status` | Session state, see below |
| `GET /state-file?slot=N` | The zipped hard disk image holding slot N's capture, named by `X-State-Filename` |
| `GET /state-screenshot?slot=N` | The PNG captured when slot N was saved, or `404` if there wasn't one |

```json
{
  "xemu_running": true,
  "active": true,
  "setup": false,
  "rom_path": "/romm/library/roms/xbox/Fable.xiso.iso",
  "rom_name": "Fable",
  "started_at": "2026-04-25T11:50:00Z",
  "launch_error": null,
  "resume_error": null,
  "hdd_error": null
}
```

`active` means xemu is reachable over QMP *and* a ROM is loaded. `setup` means xemu is up for a configuration session with no ROM. They are never both true.

The three error fields describe three different failures, and all clear on the next launch or when a session ends:

- **`launch_error`** — the game never started, because QMP never came up or the ROM would not load. `active` is false.
- **`resume_error`** — the game started, but the `load_slot` it was asked to resume from held no usable state, so it booted fresh. This is the only field that tells the frontend the player is not where they left off.
- **`hdd_error`** — the game is running on a disk that isn't its own, because the stock image was missing, the swap failed, or xemu never let go of the mounted one. It plays normally, but its state archives will carry the previous game's data.

### Write

| Endpoint | Body | Description |
|---|---|---|
| `POST /launch` | `{"rom_path": "...", "load_slot": 1–10}` | Boot the console with a ROM, optionally resuming from a slot |
| `POST /setup` | — | Boot xemu at the dashboard so it can be configured |
| `DELETE /launch` | — | End the active game or setup session and stop xemu |
| `PUT /state-file?filename=<name>.xNN` | Zipped hard disk image | Restore a state pulled from RomM |
| `POST /save-state` | `{"slot": 1–10}` | Save to a slot |
| `POST /load-state` | `{"slot": 1–10}` | Load from a slot |
| `POST /save-and-exit` | `{"slot": 0–10, "wait": true\|false}` | Save, then stop xemu |
| `POST /volume` | `{"level": 0–100}` | Set sink volume |
| `POST /mute` | `{"mute": true\|false}` or `{}` | Set or toggle mute |
| `POST /cleanup` | — | Restart selkies to flush stale gamepad sockets |

#### POST /launch

Validates that `rom_path` sits inside `ROM_ROOT`, resolves it to a disc image, then returns `200 {"status": "loading"}` and does the work on a background thread. Poll `/status` for `active: true`.

If no xemu is running, it spawns one with the disc already in the drive (`-dvd_path`) so the game boots from power-on. Only when reusing a booted instance does it insert the disc with `blockdev-change-medium` and follow with `system_reset`.

**A cold start must never be reset.** QMP starts answering about a second in, while the guest is still inside the MCPX bootrom, and a reset landing there wedges the machine: it stays `running` and burns a full core, but never draws a frame or plays a sample.

`rom_path` may be a file or a **directory**, for libraries laid out one game per folder. RomM addresses those games by folder, so the broker looks inside — the folder itself first, then one level down for the per-disc subfolders some sets use. Only XISO images count (`.iso`, including the `.xiso.iso` double extension), ranked by name so a multi-disc set boots disc 1. Dot-files are skipped and a symlink pointing outside `ROM_ROOT` is never chosen. A directory with no disc inside returns `422` listing the accepted extensions.

#### POST /setup

Boots xemu with no disc so its own menus can be reached over the stream, then arms a watchdog that stops it after `SETUP_TIMEOUT`. Returns `409` if a game is active or a launch is in progress, and `{"already": true}` if setup is already running. A real `/launch` supersedes it; `DELETE /launch` ends it early.

#### DELETE /launch

Kills xemu and clears session state, for a game or a setup session alike. An in-flight `/state-file` transfer or snapshot job gets up to `STOP_WAIT` seconds to finish first, since the transfer holds the guest paused mid-read and a save is a job a `SIGTERM` would cut in half. After that the stop wins regardless, because it is the only way out of a hung session.

#### POST /save-and-exit

`slot` defaults to 10, the autosave slot, and `0` is a legacy value remapped to it. With `wait: true` (the default) the save runs synchronously and the response carries `{"saved": <bool>, "slot": N}`; with `wait: false` it is queued and the response is `{"status": "queued"}`. If the save fails, xemu is stopped anyway and a warning is logged.

#### PUT /state-file

Restores a state pulled from RomM. Rejected while xemu is running, and rejected with `400` unless the archive holds exactly one member that is a disk image xemu could open, so the live disk is never displaced for something unbootable.

## Save states

States are QMP snapshots living inside the hard disk image, written and read through xemu's `snapshot-save` / `snapshot-load` job API. Calls block until the job finishes, so a response reflects what actually happened. Slots 1–9 are user-controlled and slot 10 is the autosave `/save-and-exit` writes; all ten work with `/save-state` and `/load-state`.

Each save goes to a fresh tag, `broker-slot-<slot>.<sequence>`, and the older tags for that slot are deleted only once the new one has landed. QEMU refuses to save onto a tag the image already holds, so the obvious implementation deletes first — and then a save that fails leaves the player with nothing at all.

### Import and resume

A QMP snapshot cannot be exported on its own. No QMP command does it, `qemu-img convert -s` drops the vmstate, and `drive-backup` skips internal snapshots. So the portable artifact is the whole hard disk image, zipped. `GET /state-file?slot=N` pauses the vCPUs, since the qcow2 is open by a live QEMU and a mid-write copy tears, zips the image, resumes, and returns it as `<rom>.xNN`.

Because every slot lives in that one image, shipping it whole would put slots 1–4 inside the archive for slot 5, and archives would grow with every save. Instead the image is rebuilt around the snapshot being served: the active disk and that one snapshot are copied into a fresh qcow2 and everything else is left behind, including clusters an interrupted job leaked. Archive size then tracks the state being saved rather than how many saves came before it. On a real image with two slots this took an archive from 83MB to 53MB, and the second number stopped climbing.

The rebuild is verified before it is used. The guest-visible bytes of both surviving mappings are compared against the original cluster by cluster and the refcounts are recomputed from the tables. Anything short of an exact match, an image the rebuild does not understand (compressed clusters, a backing file, encryption), or `STATE_TRIM=0` falls back to serving the untouched image. This can cost archive size, never a save.

To resume, RomM pushes the archive back with `PUT /state-file` while xemu is stopped, then launches with `load_slot`. The broker inserts the disc first and loads the snapshot after, so the restored machine is already running that disc.

Slot numbers are container-side QMP handles only. RomM stores states as history pruned to its own limit, not in fixed slots.

## Per-game hard disks

Every game plays off its own disk image. `HDD_STORE` holds one qcow2 per ROM; at launch the live image moves into the store under the outgoing game's name and the incoming game's image takes its place, or a fresh copy of `HDD_STOCK` does if that game has never been played. A `current` file records whose disk is mounted, so a restart cannot file an image under the wrong name.

This exists because a state archive is the whole disk image. Xbox titles write their caches and saves to the hard disk, so on one shared disk every archive carries every game ever played — in testing, a state that started at 50MB came back at 590MB after two sessions of other games had touched the same disk. Trimming cannot reach that, because those clusters belong to the active disk rather than to the snapshots the trim drops.

The swap needs xemu stopped, since QEMU holds the image open, so a launch that has to exchange disks stops any running instance and cold-boots from the disc. Launching the game already on the live disk costs nothing, and neither does a resume: a restored image is marked as belonging to whichever game launches next, which is always the game the archive came from.

Failure degrades rather than blocks. If there is no disk for the game and no usable stock image, or the swap fails and the outgoing disk goes back where it was, the game launches on the disk already mounted and `/status` reports `hdd_error`. Only a swap whose rollback also failed refuses the launch, since then there is no disk left to boot.

Nothing prunes the store — it grows by one image per game played. An image the broker cannot attribute to a game is filed as `unclaimed-N.qcow2` rather than deleted, since it may hold the only copy of an in-game save nobody pulled a state for. The shared disk from before this feature existed becomes one, as does a disk whose `current` record failed to write. Nothing reads them back automatically: play the game the image belongs to once so it files a disk under the right name, then stop the container and swap the unclaimed file into its place.

### Why there is no separate in-game save sync

PCSX2 and Dolphin expose in-game saves as files a broker can read. xemu keeps them in a FATX filesystem inside the qcow2, which would need both a qcow2 reader and a FATX reader written from scratch, and the broker is stdlib only. Save states carry the whole disk image, so in-game saves travel inside them. A state therefore restores the console as it stood, including that title's in-game saves — but only that title's, since each game has its own disk.

## State thumbnails

Frames come from pixelflux, the compositor when selkies runs in Wayland mode. `init.sh` enables its Computer Use HTTP server by writing `PIXELFLUX_CU` into the container environment before selkies starts, so thumbnails need no setup from whoever installs the mod. At save time the broker posts `{"action": "screenshot"}` and stores the PNG that comes back beside the disk image, where `GET /state-screenshot` serves it. Frames belong to the disk image they were captured from, so they travel with it when games are swapped.

What comes back is the composited output, the picture the player is actually looking at. Nothing else in the container can produce one: xemu ships without pixman so QMP has no `screendump` command, xemu creates no X window for `xwd` to read, and pixelflux implements no screencopy protocol for `grim` to use.

**The port must never be published.** The Computer Use API carries no credential and injects keyboard and mouse as well as capturing frames, so anything that can reach it drives the desktop. Unpublished, it is reachable only from inside the container, which is where the broker runs. Set `PIXELFLUX_CU` to move it off `8085`, or to `0` to turn it off.

Thumbnails need Wayland mode and a base image new enough to have the feature. pixelflux 1.6.4, which ships in `v0.8.134-ls76`, ignores `PIXELFLUX_CU`; the broker then logs a refused connection, stores no frame, and `GET /state-screenshot` returns `404`.

## Roadmap: a per-user hard disk

Disks are a fixture of the container today: one image per game, shared by whoever streams that game next, holding in-game saves for every user on the platform. That is the wrong owner — it makes one player's progress visible to the next, and each image grows without bound as users accumulate.

The idea is to make the image a per-user asset RomM stores and hands back at launch, the way a memory card follows its owner rather than living in the console. The broker would receive the player's image with the launch and return it when the session ends, keeping nothing between sessions.

Open questions before it is worth planning: how it interacts with save states, since both are the same qcow2 today; what RomM's asset model can already express; whether the handoff can be made cheap enough to sit in the launch path; and what happens when one user streams from two places at once.

## Architecture

```
Startup (init-xemu-audio)
  └── Wait for PulseAudio, then create the output/input null sinks before
      selkies starts: the base image's own setup can lose that race

Startup (init.sh)
  └── Write a broker-managed autostart (labwc + openbox): no boot-time xemu
  └── Seed xemu.toml: gamepad on port 1, fullscreen_on_startup, VULKAN on AMD
  └── Copy the stock hard disk image to /config/xemu and repoint hdd_path
  └── Enable the pixelflux capture server and chown the config dir to abc

Broker (broker.py, port 8000)
  └── POST /launch        → spawn xemu -dvd_path <rom> -qmp … (cold start, no reset)
  │                         or blockdev-change-medium + system_reset when reusing one
  └── POST /setup         → spawn xemu at the dashboard, auto-stop after SETUP_TIMEOUT
  └── DELETE /launch      → kill xemu
  └── GET  /state-file    → QMP stop → trim → zip → cont
  └── PUT  /state-file    → restore a zipped image (only while xemu is stopped)
  └── POST /save-state    → snapshot-save to a fresh tag, drop the superseded ones
  └── POST /load-state    → snapshot-load
  └── POST /save-and-exit → save to slot 10 + kill xemu
  └── POST /volume /mute  → pactl
  └── POST /cleanup       → restart selkies

RomM ←→ selkies WebRTC ←→ browser
```

## Troubleshooting

**The game never loads after `/launch`.** Poll `/status`. If `active` is still false after 60s, read `launch_error` and check the container logs for QMP errors.

**Saving times out.** Large states can take longer than the default 10s. Raise `QMP_WAIT`.

**No controllers in xemu.** The selkies joystick interposer needs the streaming session up before xemu starts, so connect through the RomM player first, then launch.

**A gameless xemu is burning CPU.** Check `/config/.config/labwc/autostart` (or the openbox one) — it should carry the `broker-managed` marker and no `AppRun` line. Delete the file and restart the container to let `init.sh` write it again.

**Settings do not survive a restart.** The xemu config directory has to be writable by `abc`. `init.sh` chowns it at startup, but that can fail on a `/config` volume with permission problems; look for the ownership line in the logs.

**States have no thumbnails.** The capture server only runs in Wayland mode on a recent enough base image. The startup log says which of those is missing.
