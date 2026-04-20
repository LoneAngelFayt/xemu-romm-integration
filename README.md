# xemu-romm-integration-mod

A [linuxserver Docker mod](https://docs.linuxserver.io/general/container-customization/#docker-mods) for [linuxserver/xemu](https://docs.linuxserver.io/images/docker-xemu/) that adds an HTTP broker for [RomM](https://github.com/rommapp/romm) streaming integration.

Enables RomM to launch Xbox original games in a remote streaming session, with full save state support via QMP, controller input, and volume/mute control.

## Prerequisites

xemu requires BIOS files and an Xbox HDD image to function. **These must be configured in the xemu settings UI before launching games via RomM.** Open the container's web interface and configure:

- **MCPX boot ROM** — `mcpx_1.0.bin`
- **Xbox BIOS** — e.g. `complex_4627v1.03.bin`
- **Xbox HDD image** — `xbox_hdd.qcow2` (xemu can generate a blank one)

Save states are stored inside the HDD image. If the HDD image is not configured, save/load state calls will fail with a QMP error.

## Features

- Launch Xbox ROMs on demand from RomM (XISO `.iso` format)
- Return to the xemu settings UI when done
- Save state support — 9 user slots + 1 autosave slot (slot 10), stored in the Xbox HDD image via QMP
- Volume and mute control via PulseAudio
- Controller support via selkies joystick interposer

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
| `BROKER_SECRET` | unset | Optional shared secret. POST/DELETE require `X-Broker-Secret` header when set. |
| `ROM_ROOT` | `/romm/library` | ROM path validation root |
| `QMP_SOCKET` | `/tmp/xemu-qmp.sock` | Path to xemu QMP Unix socket |
| `QMP_TIMEOUT` | `2.0` | QMP connect/send timeout (seconds) |
| `QMP_WAIT` | `10.0` | Max seconds to wait for savevm/loadvm to complete |
| `BROKER_LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Broker API

All write endpoints require `X-Broker-Secret: <secret>` when `BROKER_SECRET` is set.

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | `{"status": "ok"}` |
| `/status` | GET | Current session state |
| `/launch` | POST | `{"rom_path": "..."}` — launch a game |
| `/launch` | DELETE | Return to xemu settings UI |
| `/save-and-exit` | POST | Save to slot 10 (autosave) via QMP, kill game, return to dashboard |
| `/save-state` | POST | `{"slot": 1–9}` — save via QMP without stopping |
| `/load-state` | POST | `{"slot": 1–9}` — load via QMP |
| `/volume` | POST | `{"level": 0–100}` — set PulseAudio volume |
| `/mute` | POST | `{"mute": true/false}` or `{}` to toggle |

## Save States

Save states are named snapshots stored inside the Xbox HDD image (`xbox_hdd.qcow2`). The broker uses QMP (`savevm`/`loadvm`) for synchronous save/load — the API call blocks until the operation completes, so the response reliably reflects success or failure.

Slot names in the HDD image: `broker-slot-1` through `broker-slot-10`.

## Architecture

```
RomM → POST /api/streaming/sessions (platform=xbox)
     → broker POST /launch
         → _kill_xemu()
         → _drain_gamepad_sockets()
         → time.sleep(2)
         → xemu -full-screen -dvd_path <rom> -qmp unix:/tmp/xemu-qmp.sock,server,nowait
             └── selkies WebRTC ← browser (RomM player)

RomM → POST /save-and-exit
     → _qmp_command("savevm", {"name": "broker-slot-10"})  ← synchronous
     → _kill_xemu()
     → _launch_xemu(None)   ← dashboard
```
