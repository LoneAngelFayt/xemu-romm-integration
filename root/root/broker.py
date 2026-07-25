#!/usr/bin/env python3
"""broker.py — launch xemu on demand and manage it over QMP.

The broker owns the xemu process lifecycle: it spawns xemu (with the QMP
socket flag) when a ROM is launched and kills it when the session ends.
xemu is QEMU-based and busy-loops several CPU cores while idling at the
dashboard, so no gameless instance is ever kept around."""

import hmac
import io
import json
import logging
import os
import re
import shutil
import signal
import socket as _socket
import struct
import subprocess
import sys
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from threading import Thread, Lock, Timer
from urllib.parse import parse_qs, urlparse

# ── Config ────────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("BROKER_PORT", "8000"))
SECRET = os.environ.get("BROKER_SECRET", "")
ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
QMP_SOCKET = Path(os.environ.get("QMP_SOCKET", "/tmp/xemu-qmp.sock"))
QMP_TIMEOUT = float(os.environ.get("QMP_TIMEOUT", "2.0"))
QMP_WAIT = float(os.environ.get("QMP_WAIT", "10.0"))
QMP_BOOT_TIMEOUT = float(os.environ.get("QMP_BOOT_TIMEOUT", "60.0"))

# A setup session (gameless xemu for configuring the machine) auto-stops after
# this many seconds so it never idles indefinitely burning CPU.
SETUP_TIMEOUT = float(os.environ.get("SETUP_TIMEOUT", "900"))

# The hard disk image doubles as the save-state artifact: xemu writes snapshots
# into it, and there is no way to export one snapshot on its own.
HDD_IMAGE = Path(os.environ.get("HDD_IMAGE", "/config/xemu/xbox_hdd.qcow2"))
HDD_IMAGE_ENTRY = "xbox_hdd.qcow2"
STATE_FILE_MAX_BYTES = int(os.environ.get("STATE_FILE_MAX_BYTES", str(256 * 1024 * 1024)))
STATE_GET_WAIT = float(os.environ.get("STATE_GET_WAIT", "30.0"))

# Captured state frames sit beside the disk image so they outlive xemu: RomM
# asks for the frame during the pull, by which time /save-and-exit has already
# killed the process that could have drawn it.
STATE_SHOT_DIR = Path(os.environ.get("STATE_SHOT_DIR", str(HDD_IMAGE.parent)))

# DELETE /launch gives an in-flight /state-file transfer or snapshot job this
# long to finish before killing xemu anyway — the transfer holds the guest
# paused mid-read, and a SIGTERM lands mid-snapshot-save.
STOP_WAIT = float(os.environ.get("STOP_WAIT", "5.0"))

# Per-socket timeout for an HTTP request. Without one a client that sends
# headers and then stalls occupies a handler thread forever, and any exclusion
# flag that request holds is pinned until the broker restarts.
REQUEST_TIMEOUT = float(os.environ.get("BROKER_REQUEST_TIMEOUT", "60.0"))

# xemu runs as abc and must be able to write snapshots into a restored image.
_ABC_UID = int(os.environ.get("PUID", "1000"))
_ABC_GID = int(os.environ.get("PGID", "1000"))

# /opt/xemu/AppRun is a symlink to the xemu binary, so the spawned process
# reports comm "AppRun" (see _reap_stray_xemu).
XEMU_CMD = os.environ.get("XEMU_CMD", "/opt/xemu/AppRun")

# LD_PRELOAD must include the joystick interposer and the fake libudev or SDL
# never discovers the synthetic /dev/input/js* devices selkies creates.
_LD_PRELOAD = (
    os.environ.get("LD_PRELOAD")
    or "/usr/lib/selkies_joystick_interposer.so:/opt/lib/libudev.so.1.0.0-fake"
)

# Session environment xemu previously inherited from the desktop autostart;
# now that the broker spawns it, replicated here for sudo -u abc env.
ENV = {
    "DISPLAY":            os.environ.get("DISPLAY", ":1"),
    "XDG_RUNTIME_DIR":    "/config/.XDG",
    "PULSE_RUNTIME_PATH": "/defaults",
    "LD_PRELOAD":         _LD_PRELOAD,
    "HOME":               "/config",
    "USER":               "abc",
}

logging.basicConfig(
    level=getattr(
        logging, os.environ.get("BROKER_LOG_LEVEL", "INFO").upper(), logging.INFO
    ),
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_lock = Lock()
_state: dict = {
    "process": None,
    "rom_path": None,
    "rom_name": None,
    "started_at": None,
    "save_in_progress": False,
    "launch_in_progress": False,  # guards against concurrent /launch requests
    "state_file_in_progress": False,  # a /state-file GET or PUT owns the disk image
    # GET only: it reads the image with the guest paused, so it holds the QMP
    # monitor against a live xemu. A PUT runs with xemu down and must not be
    # mistaken for one.
    "state_file_reading": False,
    "launch_error": None,
    "resume_error": None,         # slot resume failed but the ROM did launch
    "setup": False,               # gameless xemu is up for configuration
    "setup_timer": None,          # threading.Timer that auto-stops setup
    # Bumped by DELETE /launch. A background launch carries the value it was
    # started with, so it can tell that the session it belongs to was stopped.
    "session_generation": 0,
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _session_superseded(generation: int) -> bool:
    """True once DELETE /launch ended the session a background job belongs to.
    Caller must hold _lock."""
    return _state["session_generation"] != generation


def _abandon_if_cancelled(generation: int, spawned: bool) -> bool:
    """Bail out of a background launch that DELETE /launch already stopped.

    The stop wins: an xemu this job spawned after the kill would otherwise
    outlive it. A reused instance is already dead, so _kill_xemu is a no-op
    there. Returns True when the caller must return immediately."""
    with _lock:
        if not _session_superseded(generation):
            return False
    log.info("Session stopped while launching — abandoning this launch")
    if spawned:
        _kill_xemu()
    return True


def _validate_rom_path(raw: str) -> Path | None:
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    return p


# ── Process lifecycle ─────────────────────────────────────────────────────────


def _claim_state_file(deadline: float, reading: bool = False) -> str | None:
    """Reserve the disk image for a /state-file transfer.

    Both directions touch the qcow2 the emulator owns, so they must not overlap
    each other, a snapshot job or a launch. Waits out an in-flight save until
    `deadline` because RomM fetches straight after POST /save-state. `reading`
    marks the GET direction, which holds the QMP monitor. Returns an error
    string on conflict, None once the flag is held by this caller."""
    while True:
        with _lock:
            if _state["state_file_in_progress"]:
                return "state-file transfer already in progress"
            if _state["launch_in_progress"]:
                return "launch in progress"
            if not _state["save_in_progress"]:
                _state["state_file_in_progress"] = True
                _state["state_file_reading"] = reading
                return None
        if time.monotonic() >= deadline:
            return "save still in progress"
        time.sleep(0.2)


def _release_state_file() -> None:
    with _lock:
        _state["state_file_in_progress"] = False
        _state["state_file_reading"] = False


def _wait_for_no_xemu(timeout: float = 3.0) -> bool:
    """Block until no xemu process remains, up to `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = False
        for name in ("AppRun", "xemu"):
            if subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0:
                alive = True
                break
        if not alive:
            return True
        time.sleep(0.1)
    return False


def _reap_stray_xemu() -> None:
    """SIGKILL any xemu process the broker does not own.

    An orphaned xemu keeps busy-looping CPU cores and holds the QMP socket
    path, so strays are reaped at broker startup and before every launch.
    -x matches the binary name exactly; comm is "AppRun" when spawned via
    the AppImage symlink, "xemu" if invoked directly.
    """
    killed = False
    for name in ("AppRun", "xemu"):
        if subprocess.run(["pkill", "-9", "-x", name], capture_output=True).returncode == 0:
            killed = True
    if killed:
        log.info("Reaped stray xemu process(es).")
        if not _wait_for_no_xemu():
            log.error("Stray xemu survived SIGKILL; launch may misbehave")


def _kill_xemu() -> None:
    """Kill the managed xemu process group. Lock is released before waiting."""
    with _lock:
        proc = _state["process"]
        _state["process"] = None

    if proc is None or proc.poll() is not None:
        return

    log.info("Stopping xemu (PID %d)...", proc.pid)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("xemu did not exit after SIGTERM — sending SIGKILL")
            os.killpg(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.error("xemu did not exit after SIGKILL — giving up")
    except ProcessLookupError:
        pass  # already gone


def _launch_xemu() -> bool:
    """Spawn xemu as abc with the QMP socket flag. Returns False on failure."""
    _kill_xemu()
    if not _wait_for_no_xemu():
        # _kill_xemu reaped the managed group, so any survivor is a stray.
        _reap_stray_xemu()

    # QEMU fails to bind if a dead socket file is left behind.
    try:
        QMP_SOCKET.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not remove stale QMP socket %s: %s", QMP_SOCKET, exc)

    cmd = [
        "sudo", "-u", "abc", "env",
        *[f"{k}={v}" for k, v in ENV.items()],
        XEMU_CMD,
        "-qmp", f"unix:{QMP_SOCKET},server,nowait",
    ]
    log.info("Launching xemu...")
    log.debug("Launching: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Own process group so killpg is clean. Not preexec_fn=os.setpgrp:
            # that runs Python between fork and exec, which is unsafe in a
            # threaded process and rejected outright on free-threaded builds.
            start_new_session=True,
        )
    except OSError as exc:
        log.error("Failed to launch xemu: %s", exc)
        return False

    with _lock:
        _state["process"] = proc
    log.info("xemu launched (PID %d)", proc.pid)
    return True


# ── QMP ───────────────────────────────────────────────────────────────────────


def _qmp_command(cmd: str, args: dict | None = None) -> dict:
    """Open a fresh QMP connection, negotiate capabilities, send one command.

    Raises OSError if the socket is unavailable.
    Raises ValueError if QMP returns an error response.
    """
    payload: dict = {"execute": cmd}
    if args:
        payload["arguments"] = args

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    buf = b""

    def recv_line() -> dict:
        nonlocal buf
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("QMP socket closed unexpectedly")
            buf += chunk
        line, _, buf = buf.partition(b"\n")
        return json.loads(line)

    try:
        sock.settimeout(QMP_TIMEOUT)
        sock.connect(str(QMP_SOCKET))
        recv_line()  # greeting
        sock.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
        recv_line()  # {"return": {}}
        sock.settimeout(QMP_WAIT)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        response = recv_line()
    except (OSError, json.JSONDecodeError) as exc:
        sock.close()
        raise OSError(f"QMP {cmd} failed: {exc}") from exc
    else:
        sock.close()

    if "error" in response:
        desc = response["error"].get("desc", str(response["error"]))
        raise ValueError(f"QMP error: {desc}")
    return response


def _qmp_available() -> bool:
    try:
        _qmp_command("query-status")
        return True
    except (OSError, ValueError):
        return False


def _qmp_wait_ready(timeout: float) -> bool:
    """Poll QMP until available or timeout. Returns True if ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _qmp_available():
            return True
        time.sleep(1.0)
    return False


def _qmp_reset_confirmed(retries: int = 3) -> bool:
    """Send system_reset and wait for the RESET event, retrying on failure."""
    for attempt in range(1, retries + 1):
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        buf = b""

        def recv_msg():
            nonlocal buf
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("QMP socket closed")
                buf += chunk
            line, _, buf = buf.partition(b"\n")
            return json.loads(line)

        try:
            sock.settimeout(QMP_TIMEOUT)
            sock.connect(str(QMP_SOCKET))
            recv_msg()  # greeting
            sock.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
            # Drain to the return under the same per-recv cap the wait loop
            # below uses: re-arming QMP_WAIT per message lets a peer that keeps
            # emitting events just under the timeout pin this thread for good.
            cap_deadline = time.monotonic() + QMP_WAIT
            while True:
                remaining = cap_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("qmp_capabilities was never acknowledged")
                sock.settimeout(remaining)
                if "return" in recv_msg():
                    break
            sock.sendall(json.dumps({"execute": "system_reset"}).encode() + b"\n")
            # Each recv is capped at what is left of the budget: a full QMP_WAIT
            # timeout on a recv entered just under the deadline would stretch the
            # real worst case to twice QMP_WAIT, times `retries`.
            deadline = time.monotonic() + QMP_WAIT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    msg = recv_msg()
                except TimeoutError:
                    break  # budget spent
                if msg.get("event") == "RESET":
                    log.debug("QMP: RESET event confirmed (attempt %d)", attempt)
                    return True
                if "return" in msg or "error" in msg:
                    continue
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("QMP: reset attempt %d/%d failed: %s", attempt, retries, exc)
        finally:
            sock.close()

        if attempt < retries:
            time.sleep(0.5)

    log.error("QMP: system_reset not confirmed after %d attempts", retries)
    return False


def _qmp_load_rom(rom_path: str) -> bool:
    try:
        _qmp_command("blockdev-change-medium", {"device": "ide0-cd1", "filename": rom_path})
    except (OSError, ValueError) as exc:
        log.error("QMP: blockdev-change-medium failed: %s", exc)
        return False
    if _qmp_reset_confirmed():
        log.info("QMP: loaded ROM %s and reset confirmed", rom_path)
        return True
    log.error("QMP: reset not confirmed after loading ROM %s", rom_path)
    return False


def _qmp_get_hdd_node() -> str:
    """Return the block node name for ide0-hd0. Raises ValueError if not found."""
    r = _qmp_command("query-block")
    for dev in r.get("return", []):
        if dev.get("device") == "ide0-hd0":
            node = dev.get("inserted", {}).get("node-name")
            if node:
                return node
    raise ValueError("ide0-hd0 block node not found")


def _qmp_snapshot_tags() -> tuple[str, set] | None:
    """The hard disk image xemu has open, and every internal snapshot tag on it.

    Returns (filename, tags). None means the query itself failed; an empty set
    would read as "this slot holds no state", so failure is never reported that
    way. The filename comes back too because the snapshots belong to whatever
    image QEMU opened, which need not be the one the broker serves."""
    try:
        r = _qmp_command("query-block")
    except (OSError, ValueError) as exc:
        log.error("QMP: query-block failed: %s", exc)
        return None
    for dev in r.get("return", []):
        if dev.get("device") != "ide0-hd0":
            continue
        image = dev.get("inserted", {}).get("image", {})
        return (
            image.get("filename", ""),
            {s.get("name", "") for s in image.get("snapshots", [])},
        )
    log.error("QMP: query-block returned no ide0-hd0 device")
    return None


_QCOW2_MAGIC = b"QFI\xfb"
# One snapshot table entry: l1_table_offset, l1_size, id_str_size, name_size,
# date_sec, date_nsec, vm_clock_nsec, vm_state_size, extra_data_size. The
# variable-length extra_data, id_str and name follow, padded to 8 bytes.
_QCOW2_SNAPSHOT_ENTRY = ">QIHHIIQII"


def _qcow2_snapshot_tags(path: Path) -> set | None:
    """Every internal snapshot tag on a qcow2, read from the file itself.

    The offline twin of _qmp_snapshot_tags. Once xemu exits nothing holds the
    image open, so the on-disk snapshot table is the authority and a state
    written by /save-and-exit stays reachable after the process is gone.

    Returns None when the file cannot be read or parsed — an empty set means
    "this image holds no snapshots", so a failure is never reported that way."""
    try:
        with path.open("rb") as fh:
            head = fh.read(72)
            if len(head) < 72 or head[:4] != _QCOW2_MAGIC:
                log.error("state-file: %s is not a qcow2", path)
                return None
            # qcow2 header: nb_snapshots is a u32 at byte 60, snapshots_offset
            # a u64 at byte 64.
            count = int.from_bytes(head[60:64], "big")
            if not count:
                return set()
            fh.seek(int.from_bytes(head[64:72], "big"))
            tags = set()
            for _ in range(count):
                entry = fh.read(40)
                if len(entry) < 40:
                    raise ValueError("snapshot table ends mid-entry")
                _, _, id_len, name_len, _, _, _, _, extra_len = struct.unpack(
                    _QCOW2_SNAPSHOT_ENTRY, entry
                )
                fh.read(extra_len)
                fh.read(id_len)
                name = fh.read(name_len)
                if len(name) < name_len:
                    raise ValueError("snapshot table ends mid-name")
                tags.add(name.decode("utf-8", "replace"))
                fh.read(-(extra_len + id_len + name_len) % 8)
            return tags
    except (OSError, ValueError, struct.error) as exc:
        log.error("state-file: could not read snapshots from %s: %s", path, exc)
        return None


def _same_image(qmp_filename: str, path: Path) -> bool:
    """True when the image QEMU reports and `path` are the same file.

    hdd_path is set by the user in xemu.toml while HDD_IMAGE is broker config,
    so the two drift apart the moment someone repoints one of them."""
    if not qmp_filename:
        return False
    try:
        return Path(qmp_filename).resolve() == path.resolve()
    except OSError:
        return False


def _qmp_pause() -> bool:
    """Pause the guest so the qcow2 stops changing under a read."""
    try:
        _qmp_command("stop")
        return True
    except (OSError, ValueError) as exc:
        log.error("QMP: stop failed: %s", exc)
        return False


def _qmp_resume() -> bool:
    try:
        _qmp_command("cont")
        return True
    except (OSError, ValueError) as exc:
        log.error("QMP: cont failed: %s", exc)
        return False


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + kind + data + crc.to_bytes(4, "big")


def _ppm_to_png(data: bytes) -> bytes | None:
    """Re-encode a binary PPM (P6) frame as PNG.

    xemu's screendump writes PPM only: the QAPI ImageFormat enum is there, but
    the binary links no libpng, so asking for png fails at runtime. RomM drops
    anything that is not a PNG, so the conversion happens here."""
    if not data.startswith(b"P6"):
        log.warning("state-shot: capture is not a P6 PPM")
        return None
    # Three whitespace-separated numbers follow the magic, with '#' comments
    # allowed between them.
    fields, pos = [], 2
    while len(fields) < 3:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            while pos < len(data) and data[pos:pos + 1] != b"\n":
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace():
            pos += 1
        if pos == start:
            log.warning("state-shot: PPM header is truncated")
            return None
        try:
            fields.append(int(data[start:pos]))
        except ValueError:
            log.warning("state-shot: PPM header holds a non-numeric field")
            return None
    width, height, maxval = fields
    pos += 1  # exactly one whitespace byte closes the header
    if maxval != 255:
        log.warning("state-shot: PPM maxval %d is not 8-bit", maxval)
        return None
    stride = width * 3
    pixels = data[pos:]
    if width <= 0 or height <= 0 or len(pixels) < stride * height:
        log.warning("state-shot: PPM pixel data is short for %dx%d", width, height)
        return None
    # PPM rows are already PNG's truecolor layout; each scanline just needs a
    # leading filter byte, and 0 means "no filter".
    raw = b"".join(
        b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height)
    )
    return (
        _PNG_MAGIC
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )


def _state_shot_path(slot: int) -> Path:
    return STATE_SHOT_DIR / f"state-slot-{slot}.png"


def _delete_state_shot(slot: int) -> None:
    """Drop a slot's frame so an overwritten save never keeps the old one."""
    try:
        _state_shot_path(slot).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("state-shot: could not remove the frame for slot %d: %s", slot, exc)


def _delete_all_state_shots() -> None:
    """Drop every frame, for when the image they were captured from is gone."""
    try:
        shots = list(STATE_SHOT_DIR.glob("state-slot-*.png"))
    except OSError as exc:
        log.warning("state-shot: could not list %s: %s", STATE_SHOT_DIR, exc)
        return
    for shot in shots:
        try:
            shot.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("state-shot: could not remove %s: %s", shot, exc)


def _capture_state_shot(slot: int) -> bool:
    """Save the current frame for `slot` as a PNG beside the disk image.

    Taken at save time on purpose: RomM fetches the frame after the state pull,
    and by then /save-and-exit has killed xemu, so capturing on request would
    always come too late."""
    ppm = Path(f"/tmp/xemu-shot-{slot}.ppm")
    try:
        # The QMP reply only lands once QEMU has finished writing the file.
        _qmp_command("screendump", {"filename": str(ppm)})
        png = _ppm_to_png(ppm.read_bytes())
    except (OSError, ValueError) as exc:
        log.warning("state-shot: capture for slot %d failed: %s", slot, exc)
        return False
    finally:
        try:
            ppm.unlink(missing_ok=True)
        except OSError:
            pass
    if png is None:
        return False
    target = _state_shot_path(slot)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        STATE_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(png)
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("state-shot: could not write %s: %s", target, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    log.info("state-shot: captured slot %d (%d bytes)", slot, len(png))
    return True


def _zip_hdd_image() -> bytes | None:
    """Zip the hard disk image into memory.

    With xemu up the caller must pause the guest first: this is a qcow2 a live
    QEMU holds open, and a copy taken mid-write can catch torn metadata. With
    xemu gone the image is quiescent and no pause is possible or needed."""
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(HDD_IMAGE, HDD_IMAGE_ENTRY)
    except OSError as exc:
        log.error("state-file: could not zip %s: %s", HDD_IMAGE, exc)
        return None
    return buf.getvalue()


def _restore_hdd_image(content: bytes) -> str | None:
    """Replace the hard disk image with the one inside a pulled state archive.

    Returns an error string, or None on success. Writes to a temp file and
    renames, so a truncated transfer cannot leave a corrupt disk behind."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return "body is not a zip archive"
    with zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if len(members) != 1 or members[0].filename != HDD_IMAGE_ENTRY:
            return f"archive must hold exactly one {HDD_IMAGE_ENTRY} member"
        if members[0].file_size > STATE_FILE_MAX_BYTES:
            return "archive exceeds size limit when extracted"
        tmp = HDD_IMAGE.parent / f".{HDD_IMAGE.name}.tmp"

        def drop_tmp() -> None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

        try:
            HDD_IMAGE.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(members[0]) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.chown(tmp, _ABC_UID, _ABC_GID)
            os.replace(tmp, HDD_IMAGE)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            # A CRC or deflate-stream mismatch only surfaces while decompressing,
            # long after the header checks above passed.
            drop_tmp()
            return f"archive member is corrupt: {exc}"
        except OSError as exc:
            drop_tmp()
            return f"could not write the hard disk image: {exc}"
    # The frames describe snapshots in the image just replaced, so keeping them
    # would caption the restored states with the previous session's pictures.
    _delete_all_state_shots()
    return None


def _qmp_snapshot(cmd: str, tag: str) -> bool:
    """Run snapshot-save or snapshot-load as an async job; wait for completion."""
    try:
        node = _qmp_get_hdd_node()
    except (OSError, ValueError) as exc:
        log.error("QMP: cannot get HDD node for %s: %s", cmd, exc)
        return False

    job_id = tag
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    buf = b""

    def recv_msg():
        nonlocal buf
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("QMP socket closed")
            buf += chunk
        line, _, buf = buf.partition(b"\n")
        return json.loads(line)

    def send_cmd(execute, args=None):
        payload = {"execute": execute}
        if args:
            payload["arguments"] = args
        sock.sendall(json.dumps(payload).encode() + b"\n")
        # The whole reply wait is capped, not each recv: a peer emitting async
        # events just under the timeout would otherwise re-arm QMP_WAIT forever
        # and pin this thread.
        cmd_deadline = time.monotonic() + QMP_WAIT
        while True:
            remaining = cmd_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"QMP {execute} did not return within {QMP_WAIT:.0f}s")
            sock.settimeout(remaining)
            msg = recv_msg()
            if "return" in msg:
                return msg
            if "error" in msg:
                raise ValueError(msg["error"].get("desc", str(msg["error"])))
            # async event — keep draining

    try:
        sock.settimeout(QMP_TIMEOUT)
        sock.connect(str(QMP_SOCKET))
        recv_msg()  # greeting
        sock.settimeout(QMP_WAIT)
        send_cmd("qmp_capabilities")

        # Dismiss any previously stuck job with this ID before starting
        try:
            send_cmd("job-dismiss", {"id": job_id})
        except (OSError, ValueError):
            pass

        args = {"job-id": job_id, "tag": tag, "devices": [node]}
        if cmd != "snapshot-delete":
            args["vmstate"] = node
        send_cmd(cmd, args)

        # Wait for the job to conclude. Each recv is capped at what is left of
        # the budget: a full-QMP_WAIT timeout on a recv entered just under the
        # deadline would stretch the real worst case to twice QMP_WAIT.
        deadline = time.monotonic() + QMP_WAIT
        concluded = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                msg = recv_msg()
            except TimeoutError:
                break  # budget spent; fall through to the job-cancel path
            if (msg.get("event") == "JOB_STATUS_CHANGE"
                    and msg.get("data", {}).get("id") == job_id
                    and msg.get("data", {}).get("status") == "concluded"):
                concluded = True
                break

        # The wait loop leaves a near-zero timeout behind; the teardown commands
        # below need the full budget again.
        sock.settimeout(QMP_WAIT)

        if not concluded:
            try:
                send_cmd("job-cancel", {"id": job_id})
            except (OSError, ValueError):
                pass
            raise OSError("Snapshot job timed out")

        jobs = send_cmd("query-jobs")
        error = None
        for job in jobs.get("return", []):
            if job.get("id") == job_id:
                error = job.get("error")
                break

        try:
            send_cmd("job-dismiss", {"id": job_id})
        except (OSError, ValueError):
            pass

        if error:
            raise ValueError(error)
        return True

    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.error("QMP: %s %s failed: %s", cmd, tag, exc)
        return False
    finally:
        sock.close()


def _qmp_save_state(slot: int) -> bool:
    tag = f"broker-slot-{slot}"
    _qmp_snapshot("snapshot-delete", tag)  # remove stale snapshot; ignore failure
    ok = _qmp_snapshot("snapshot-save", tag)
    if ok:
        log.info("QMP: snapshot saved %s", tag)
        # Best-effort: a missing frame costs the state its thumbnail and nothing
        # else, but a stale one would show the wrong save.
        if not _capture_state_shot(slot):
            _delete_state_shot(slot)
    else:
        log.error("QMP: snapshot save %s failed", tag)
    return ok


def _qmp_load_state(slot: int) -> bool:
    tag = f"broker-slot-{slot}"
    ok = _qmp_snapshot("snapshot-load", tag)
    if ok:
        log.info("QMP: snapshot loaded %s", tag)
    else:
        log.error("QMP: snapshot load %s failed", tag)
    return ok


# ── ROM loading (background) ──────────────────────────────────────────────────


def _do_load_rom(rom_path: str, load_slot: int | None = None,
                 generation: int | None = None) -> None:
    """Ensure xemu is running, inject ROM, update state, optionally resume from
    a slot. Runs in a background thread.

    `generation` is the session marker read when the launch was claimed; a
    DELETE /launch bumps it, and every write below is skipped once it differs
    so a stopped session is never resurrected by this thread."""
    try:
        with _lock:
            if generation is None:
                generation = _state["session_generation"]
            _state["launch_error"] = None
            _state["resume_error"] = None

        # Reuse a live instance (disc inject + reset is much faster than a
        # cold boot); spawn one otherwise.
        spawned = False
        if not _qmp_available():
            if not _launch_xemu():
                with _lock:
                    if not _session_superseded(generation):
                        _state["launch_error"] = "Failed to spawn xemu — see container logs"
                return
            spawned = True

        log.info("Waiting for xemu QMP (up to %.0fs)...", QMP_BOOT_TIMEOUT)
        if not _qmp_wait_ready(QMP_BOOT_TIMEOUT):
            log.error("QMP not available after %.0fs — ROM load aborted", QMP_BOOT_TIMEOUT)
            with _lock:
                if not _session_superseded(generation):
                    _state["launch_error"] = (
                        f"xemu QMP not available after {QMP_BOOT_TIMEOUT:.0f}s — ROM load aborted"
                    )
            # Reap only what this launch started: an instance we merely reused
            # is still running someone's game. A discless xemu left behind
            # busy-loops CPU cores with nothing to reap it.
            if spawned:
                _kill_xemu()
            return

        # The stop may have landed while xemu was booting; inserting the disc
        # now would bring the session the user just ended back to life.
        if _abandon_if_cancelled(generation, spawned):
            return

        ok = _qmp_load_rom(rom_path)
        # Load after the ROM is in the drive: the snapshot restores a machine
        # that was already running this disc.
        if ok and load_slot is not None:
            # A resume is a QMP snapshot job just like a save, and two jobs
            # against the shared vmstate corrupt it — hold the same flag
            # /save-state holds. The generation is re-read under that lock so a
            # stop or a save-and-exit landing while the disc went in wins.
            with _lock:
                resuming = not _session_superseded(generation)
                if resuming:
                    _state["save_in_progress"] = True
            if resuming:
                try:
                    resumed = _qmp_load_state(load_slot)
                finally:
                    with _lock:
                        _state["save_in_progress"] = False
                if not resumed:
                    log.error("Resume failed: no usable state in slot %d", load_slot)
                    # The game did boot, just from scratch — recorded separately
                    # from launch_error so /status still reports an active session.
                    with _lock:
                        if not _session_superseded(generation):
                            _state["resume_error"] = (
                                f"no usable state in slot {load_slot} — booted fresh"
                            )
        with _lock:
            superseded = _session_superseded(generation)
            if superseded:
                pass  # handled below, outside the lock
            elif ok:
                _state["rom_path"] = rom_path
                _state["rom_name"] = Path(rom_path).stem
                _state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            else:
                _state["launch_error"] = "Failed to load ROM into xemu — see container logs"
                log.error("ROM load failed for %s", rom_path)
        if superseded:
            # A disc went into an xemu the user had already stopped.
            log.info("Session stopped mid-launch — discarding the loaded ROM")
            _kill_xemu()
        elif not ok and spawned:
            # Same reasoning as the QMP-timeout branch above: this xemu has no
            # disc and no session, and a gameless one busy-loops CPU cores with
            # nothing left to reap it. A reused instance is someone else's game.
            log.info("ROM load failed — stopping the xemu this launch spawned")
            _kill_xemu()
    finally:
        with _lock:
            _state["launch_in_progress"] = False


# ── Setup session ─────────────────────────────────────────────────────────────


def _cancel_setup_watchdog() -> None:
    """Cancel and drop the setup auto-expiry timer. Caller need not hold _lock."""
    with _lock:
        timer = _state["setup_timer"]
        _state["setup_timer"] = None
    if timer is not None:
        timer.cancel()


def _arm_setup_watchdog() -> None:
    """Start the timer that stops an idle setup session after SETUP_TIMEOUT."""
    _cancel_setup_watchdog()
    timer = Timer(SETUP_TIMEOUT, _setup_expired)
    timer.daemon = True
    with _lock:
        _state["setup_timer"] = timer
    timer.start()


def _setup_expired() -> None:
    """Watchdog callback: stop xemu if a setup session is still the live state.
    A ROM launch or explicit stop supersedes setup, so re-check under the lock;
    a cancel/fire race is then harmless."""
    with _lock:
        if not _state["setup"] or _state["rom_path"] is not None:
            return
        _state["setup"] = False
        _state["setup_timer"] = None
        # The session is gone; leaving started_at behind makes /status report a
        # start time for an xemu that is about to be killed.
        _state["started_at"] = None
    log.info("Setup session timed out after %.0fs — stopping xemu", SETUP_TIMEOUT)
    _kill_xemu()


def _end_setup() -> None:
    """Clear setup mode and cancel its watchdog. Does not stop xemu; the caller
    decides whether xemu keeps running (ROM takeover) or is killed (explicit stop)."""
    _cancel_setup_watchdog()
    with _lock:
        _state["setup"] = False


def _do_setup(generation: int | None = None) -> None:
    """Boot xemu with no disc so the user can configure it, then arm the
    auto-expiry watchdog. Runs in a background thread.

    `generation` carries the same stop marker a ROM launch uses: a DELETE
    /launch must not be followed by a re-armed watchdog."""
    try:
        with _lock:
            if generation is None:
                generation = _state["session_generation"]

        spawned = False
        if not _qmp_available():
            if not _launch_xemu():
                with _lock:
                    _state["setup"] = False
                    if not _session_superseded(generation):
                        _state["launch_error"] = "Failed to spawn xemu — see container logs"
                return
            spawned = True

        log.info("Waiting for xemu QMP (up to %.0fs)...", QMP_BOOT_TIMEOUT)
        if not _qmp_wait_ready(QMP_BOOT_TIMEOUT):
            log.error("QMP not available after %.0fs — setup aborted", QMP_BOOT_TIMEOUT)
            with _lock:
                _state["setup"] = False
                if not _session_superseded(generation):
                    _state["launch_error"] = (
                        f"xemu QMP not available after {QMP_BOOT_TIMEOUT:.0f}s — setup aborted"
                    )
            # Reap only what this setup started: an instance we merely reused is
            # still running someone's game.
            if spawned:
                _kill_xemu()
            return

        if _abandon_if_cancelled(generation, spawned):
            return

        # No disc inserted: xemu sits at the dashboard / config UI. Recheck and
        # commit under one lock hold: a DELETE landing between them would leave
        # a stale started_at and a watchdog armed for a torn-down session.
        with _lock:
            superseded = _session_superseded(generation)
            if not superseded:
                _state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if superseded:
            log.info("Session stopped mid-setup — leaving no setup session behind")
            if spawned:
                _kill_xemu()
            return

        _arm_setup_watchdog()
        log.info("Setup session ready — xemu at the dashboard (auto-stops in %.0fs)", SETUP_TIMEOUT)
    finally:
        with _lock:
            _state["launch_in_progress"] = False


# ── PulseAudio ────────────────────────────────────────────────────────────────

_PACTL_CMD = [
    "sudo", "-u", "abc",
    "env", "PULSE_RUNTIME_PATH=/defaults", "HOME=/config", "USER=abc",
]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl as abc so it connects to abc's PulseAudio instance.

    A hung or missing pactl is reported as a non-zero CompletedProcess (rather
    than raising) so the /volume and /mute handlers return a 500 instead of
    dropping the connection with an unhandled exception."""
    cmd = _PACTL_CMD + ["pactl"] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        log.error("pactl timed out: %s", " ".join(args))
        return subprocess.CompletedProcess(cmd, 124, "", "pactl timed out")
    except OSError as exc:
        log.error("pactl failed to run: %s", exc)
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _pactl_get_mute() -> bool | None:
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
        log.error("pactl get-sink-mute failed (rc=%s): %s",
                  result.returncode, result.stderr.strip())
        return None
    return result.stdout.strip().endswith("yes")


def _cleanup_sockets():
    """Restart selkies to flush all stale gamepad connections."""
    log.info("Socket cleanup: restarting selkies...")
    result = subprocess.run(["pkill", "-15", "-f", "selkies"], capture_output=True)
    if result.returncode == 0:
        log.info("Socket cleanup: selkies stopped, s6 will restart it shortly.")
    else:
        log.warning("Socket cleanup: selkies not found or already stopped.")


# ── HTTP handler ──────────────────────────────────────────────────────────────


class BrokerHandler(BaseHTTPRequestHandler):
    # socketserver applies this to the connection in setup(). Without it a
    # client can hold a handler thread (and any flag that request claimed)
    # open forever by never finishing its request.
    timeout = REQUEST_TIMEOUT

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _check_secret(self) -> bool:
        if not SECRET:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Broker-Secret", ""), SECRET
        )

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", 0)), 64 * 1024))
        except ValueError:
            length = 0
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def _get_state_file(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            slot = int(query.get("slot", ["0"])[0])
        except ValueError:
            self._send_json(400, {"error": "slot must be an integer"})
            return
        if not (1 <= slot <= 10):
            self._send_json(400, {"error": "slot must be 1-10"})
            return

        # RomM fetches straight after POST /save-state, whose snapshot job runs
        # in the background. Serving mid-job would ship an image without the
        # capture in it.
        conflict = _claim_state_file(time.monotonic() + STATE_GET_WAIT, reading=True)
        if conflict is not None:
            self._send_json(409, {"error": conflict})
            return
        try:
            self._serve_state_file(slot)
        finally:
            _release_state_file()

    def _get_state_screenshot(self):
        """Serve the frame captured when the slot was saved.

        Deliberately independent of QMP: RomM asks for this after the pull, with
        xemu already gone, so the answer has to come off disk."""
        query = parse_qs(urlparse(self.path).query)
        try:
            slot = int(query.get("slot", ["0"])[0])
        except ValueError:
            self._send_json(400, {"error": "slot must be an integer"})
            return
        if not (1 <= slot <= 10):
            self._send_json(400, {"error": "slot must be 1-10"})
            return
        try:
            content = _state_shot_path(slot).read_bytes()
        except OSError:
            # Also the normal answer for a state saved before frames were kept.
            self._send_json(404, {"error": "no frame for slot", "slot": slot})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_state_file(self, slot: int):
        """Zip and serve the disk image. Caller holds the state-file flag.

        With xemu up the guest is paused around the read. With xemu gone there is
        nothing to pause and the snapshot table is read off the image directly —
        which is what /save-and-exit needs, since it kills xemu before RomM pulls
        and a live-QMP requirement stranded every exit save inside the container."""
        live = _qmp_available()
        if live:
            queried = _qmp_snapshot_tags()
            if queried is None:
                self._send_json(503, {"error": "could not query xemu for saved states"})
                return
            open_image, tags = queried
            # The snapshots were read off the image xemu has open; zipping
            # HDD_IMAGE when hdd_path points somewhere else would serve an
            # archive that does not contain the capture, and a wrong archive is
            # worse than an error.
            if not _same_image(open_image, HDD_IMAGE):
                log.error("state-file: xemu has %s open but the broker serves %s — refusing",
                          open_image or "<unknown>", HDD_IMAGE)
                self._send_json(500, {
                    "error": "xemu has a different hard disk image open than the broker serves",
                    "xemu_image": open_image,
                    "broker_image": str(HDD_IMAGE),
                })
                return
        else:
            # Read from the very image that is about to be zipped, so a hit here
            # already proves the archive carries the capture. That is what
            # _same_image buys on the live path, where the tags come from xemu.
            tags = _qcow2_snapshot_tags(HDD_IMAGE)
            if tags is None:
                self._send_json(
                    503, {"error": "could not read saved states from the disk image"}
                )
                return
        if f"broker-slot-{slot}" not in tags:
            self._send_json(404, {"error": "no state for slot", "slot": slot})
            return

        # Zipping happens entirely in memory, so an image already past the limit
        # is refused before a compressed copy of it is built. The uncompressed
        # size is the right bound: _restore_hdd_image rejects a member over the
        # same limit, so such an archive could never be pushed back anyway.
        try:
            image_size = HDD_IMAGE.stat().st_size
        except OSError as exc:
            log.error("state-file: could not stat %s: %s", HDD_IMAGE, exc)
            self._send_json(500, {"error": "could not read the hard disk image"})
            return
        if image_size > STATE_FILE_MAX_BYTES:
            log.error("state-file: %s is %d bytes — over the limit, not zipping",
                      HDD_IMAGE, image_size)
            self._send_json(413, {"error": "state file exceeds size limit"})
            return

        with _lock:
            rom_name = _state["rom_name"]

        still_paused = False
        if not live:
            content = _zip_hdd_image()
            # A /launch slipping through would put xemu back on the image
            # mid-zip, and half of that archive predates the reopen.
            if _qmp_available():
                log.error("state-file: xemu started while slot %d was being read", slot)
                self._send_json(
                    409, {"error": "xemu started while the disk image was being read"}
                )
                return
        elif not _qmp_pause():
            self._send_json(503, {"error": "could not pause xemu to read the disk image"})
            return
        else:
            try:
                content = _zip_hdd_image()
            finally:
                if not _qmp_resume():
                    # A DELETE /launch can kill xemu mid-read, and a dead process
                    # cannot be "stuck paused" — only report that for one that is
                    # still there.
                    if _qmp_available():
                        # The guest is stuck paused and the session is unusable
                        # until it is stopped, so the caller is told rather than
                        # served a bare 200.
                        still_paused = True
                        log.error("state-file: xemu stayed paused after reading slot %d", slot)
                    else:
                        log.info("state-file: xemu was stopped while reading slot %d", slot)

        if content is None:
            self._send_json(500, {"error": "could not read the hard disk image"})
            return
        # Authoritative check on what actually goes over the wire: the image can
        # grow between the stat above and the read.
        if len(content) > STATE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "state file exceeds size limit"})
            return

        # Header values must be latin-1; ROM stems can be anything.
        safe_name = "".join(
            c for c in (rom_name or "xemu") if c.isascii() and c.isprintable()
        ).strip() or "xemu"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-State-Filename", f"{safe_name}.x{slot:02d}")
        if still_paused:
            self.send_header("X-Xemu-Paused", "true")
        self.end_headers()
        self.wfile.write(content)
        log.info("state-file: served slot %d as %s.x%02d (%d bytes)",
                 slot, safe_name, slot, len(content))

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        # /health stays open for container healthchecks; all other GETs
        # require the shared secret, matching POST/DELETE.
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/status":
            with _lock:
                busy = _state["save_in_progress"] or _state["state_file_reading"]
            # xemu serves one QMP client at a time, so probing during a snapshot
            # job or a paused state-file read stalls for QMP_TIMEOUT and then
            # reports a live session as down. Either one implies a live xemu. A
            # /state-file PUT is deliberately not covered: it runs with xemu
            # down, so it still gets a real probe.
            xemu_up = True if busy else _qmp_available()
            with _lock:
                rom_path = _state["rom_path"]
                rom_name = _state["rom_name"]
                started_at = _state["started_at"]
                launch_error = _state["launch_error"]
                resume_error = _state["resume_error"]
                setup = _state["setup"]
            self._send_json(200, {
                "xemu_running": xemu_up,
                "active": xemu_up and rom_path is not None,
                "setup": xemu_up and setup and rom_path is None,
                "rom_path": rom_path,
                "rom_name": rom_name,
                "started_at": started_at,
                "launch_error": launch_error,
                "resume_error": resume_error,
            })
            return

        path = urlparse(self.path).path
        if path == "/state-file":
            self._get_state_file()
            return

        if path == "/state-screenshot":
            self._get_state_screenshot()
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/cleanup":
            Thread(target=_cleanup_sockets, daemon=True).start()
            self._send_json(200, {"status": "cleanup started"})
            return

        if self.path == "/launch":
            body = self._read_body()
            raw_path = body.get("rom_path", "").strip()
            if not raw_path:
                self._send_json(400, {"error": "rom_path is required"})
                return
            rom_path = _validate_rom_path(raw_path)
            if rom_path is None:
                self._send_json(400, {
                    "error": "rom_path must be within ROM_ROOT",
                    "rom_root": str(ROM_ROOT),
                })
                return
            if not rom_path.exists():
                self._send_json(422, {"error": "rom_path does not exist", "path": str(rom_path)})
                return
            load_slot = body.get("load_slot")
            if load_slot is not None and (
                not isinstance(load_slot, int) or not (1 <= load_slot <= 10)
            ):
                self._send_json(400, {"error": "load_slot must be 1-10"})
                return
            # Check save_in_progress and claim launch_in_progress in one lock
            # acquisition so a save can't start in the gap and be killed
            # mid-snapshot by the launch.
            with _lock:
                if _state["save_in_progress"]:
                    self._send_json(409, {"error": "save in progress"})
                    return
                if _state["state_file_in_progress"]:
                    self._send_json(409, {"error": "state-file transfer in progress"})
                    return
                if _state["launch_in_progress"]:
                    self._send_json(409, {"error": "launch already in progress"})
                    return
                _state["launch_in_progress"] = True
                generation = _state["session_generation"]
            # A real launch supersedes any setup session: cancel its watchdog and
            # clear the flag, then reuse the live xemu to insert the disc.
            _end_setup()
            Thread(
                target=_do_load_rom, args=(str(rom_path), load_slot, generation), daemon=True
            ).start()
            self._send_json(200, {
                "status": "loading",
                "rom_path": str(rom_path),
                "load_slot": load_slot,
            })
            return

        if self.path == "/setup":
            with _lock:
                if _state["rom_path"] is not None:
                    self._send_json(409, {"error": "a game session is active"})
                    return
                if _state["state_file_in_progress"]:
                    self._send_json(409, {"error": "state-file transfer in progress"})
                    return
                if _state["launch_in_progress"]:
                    self._send_json(409, {"error": "launch already in progress"})
                    return
                if _state["setup"]:
                    self._send_json(200, {"status": "setup", "already": True})
                    return
                _state["setup"] = True
                _state["launch_in_progress"] = True
                _state["launch_error"] = None
                _state["resume_error"] = None
                generation = _state["session_generation"]
            Thread(target=_do_setup, args=(generation,), daemon=True).start()
            self._send_json(200, {"status": "starting setup", "timeout": SETUP_TIMEOUT})
            return

        if self.path == "/save-state":
            # Body first, flag second: claiming across a client read lets one
            # request that stalls mid-body pin save_in_progress for good.
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 10):
                self._send_json(400, {"error": "slot must be 1–10"})
                return
            with _lock:
                if _state["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _state["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                if _state["state_file_in_progress"]:
                    self._send_json(409, {"error": "state-file transfer in progress"})
                    return
                # A launch may be mid-resume, which is a snapshot job on the
                # same vmstate this save would write.
                if _state["launch_in_progress"]:
                    self._send_json(409, {"error": "launch in progress"})
                    return
                _state["save_in_progress"] = True

            def _bg_save(s):
                try:
                    _qmp_save_state(s)
                finally:
                    with _lock:
                        _state["save_in_progress"] = False

            Thread(target=_bg_save, args=(slot,), daemon=True).start()
            self._send_json(200, {"status": "saving", "slot": slot})
            return

        if self.path == "/load-state":
            # Body first, flag second: claiming across a client read lets one
            # request that stalls mid-body pin save_in_progress for good.
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 10):
                self._send_json(400, {"error": "slot must be 1–10"})
                return
            with _lock:
                if _state["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _state["save_in_progress"]:
                    self._send_json(409, {"error": "save in progress"})
                    return
                if _state["state_file_in_progress"]:
                    self._send_json(409, {"error": "state-file transfer in progress"})
                    return
                # A launch resuming from a slot is running a snapshot job of its
                # own against this same vmstate.
                if _state["launch_in_progress"]:
                    self._send_json(409, {"error": "launch in progress"})
                    return
                # Hold the same flag a save uses: a load is a QMP snapshot job
                # too, and two concurrent jobs on the shared vmstate corrupt it.
                _state["save_in_progress"] = True
            try:
                ok = _qmp_load_state(slot)
            finally:
                with _lock:
                    _state["save_in_progress"] = False
            self._send_json(
                200 if ok else 503,
                {"status": "ok" if ok else "error", "loaded": ok, "slot": slot},
            )
            return

        if self.path == "/save-and-exit":
            # Body first, flag second: claiming across a client read lets one
            # request that stalls mid-body pin save_in_progress for good.
            body = self._read_body()
            slot = body.get("slot", 10)
            if not isinstance(slot, int) or not (0 <= slot <= 10):
                self._send_json(400, {"error": "slot must be 0–10"})
                return
            # Slot 0 is a legacy value meaning "use the autosave slot".
            if slot == 0:
                slot = 10
            wait = body.get("wait", True)

            with _lock:
                if _state["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _state["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                if _state["state_file_in_progress"]:
                    self._send_json(409, {"error": "state-file transfer in progress"})
                    return
                _state["save_in_progress"] = True
                # This ends the session, so like DELETE /launch it supersedes an
                # in-flight launch: without the bump the launch would write its
                # rom_path back over the session the user just exited, and its
                # slot resume would run a snapshot job against the save below.
                _state["session_generation"] += 1

            def _save_then_exit(s: int) -> bool:
                ok = False
                try:
                    ok = _qmp_save_state(s)
                    if not ok:
                        log.warning("save-and-exit: save failed (slot %d) — exiting anyway", s)
                except Exception as exc:
                    log.error("save-and-exit: unexpected error: %s", exc)
                finally:
                    # Drop the session before the kill, not after. The snapshot
                    # job is over by here, and /status reads save_in_progress as
                    # proof xemu is up, so holding it across the SIGTERM wait
                    # reports a session that is being torn down as still active.
                    with _lock:
                        _state["save_in_progress"] = False
                        _state["rom_path"] = None
                        _state["rom_name"] = None
                        _state["started_at"] = None
                        # The session ended cleanly, so /status must not keep
                        # reporting errors from it — same as DELETE /launch.
                        _state["launch_error"] = None
                        _state["resume_error"] = None
                # Kill rather than return to the dashboard: an idle xemu
                # busy-loops several CPU cores under software rendering.
                _kill_xemu()
                return ok

            if wait:
                ok = _save_then_exit(slot)
                self._send_json(200, {"status": "ok", "saved": ok, "slot": slot})
            else:
                Thread(target=_save_then_exit, args=(slot,), daemon=True).start()
                self._send_json(200, {"status": "queued", "slot": slot})
            return

        if self.path == "/volume":
            body = self._read_body()
            level = body.get("level")
            if not isinstance(level, int) or not (0 <= level <= 100):
                self._send_json(400, {"error": "level must be an integer 0–100"})
                return
            result = _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{level}%")
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            self._send_json(200, {"status": "ok", "level": level})
            return

        if self.path == "/mute":
            body = self._read_body()
            if "mute" in body:
                mute = body["mute"]
                # Bare truthiness would mute on the JSON string "false", i.e. do
                # the opposite of what the caller asked for.
                if not isinstance(mute, bool):
                    self._send_json(400, {"error": "mute must be a boolean"})
                    return
                mute_arg = "1" if mute else "0"
            else:
                mute_arg = "toggle"
            result = _pactl("set-sink-mute", "@DEFAULT_SINK@", mute_arg)
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            mute_state = _pactl_get_mute()
            self._send_json(200, {"status": "ok", "mute": mute_state})
            return

        self._send_json(404, {"error": "not found"})

    def do_PUT(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        parsed = urlparse(self.path)
        if parsed.path != "/state-file":
            self._send_json(404, {"error": "not found"})
            return

        # Basename only. The name came from a previous GET and is written back
        # verbatim, so path parts are rejected rather than normalised.
        filename = Path(parse_qs(parsed.query).get("filename", [""])[0]).name
        if filename.startswith(".") or not re.fullmatch(r".+\.x\d{2}", filename):
            self._send_json(400, {"error": "filename must be a <name>.xNN basename"})
            return

        # Restoring under a live QEMU would corrupt the image it has open. RomM
        # pushes before launch, so this never blocks the legitimate path.
        if _qmp_available():
            self._send_json(409, {
                "error": "xemu is running; stop the session before restoring a state",
            })
            return

        # Body first, flag second: a client that announces a Content-Length and
        # then stalls would otherwise pin state_file_in_progress and 409 every
        # later /launch, /setup, /save-state and /state-file. The archive is
        # held in memory either way, so nothing is paid for reading it first.
        content = self._read_state_body()
        if content is None:
            return  # the error response is already sent

        # Two restores share one temp file, so they must not interleave; a save
        # or launch must not run against a disk image being replaced either.
        conflict = _claim_state_file(time.monotonic())
        if conflict is not None:
            self._send_json(409, {"error": conflict})
            return
        try:
            # The check above is minutes old for a large upload — a launch can
            # have started and finished inside the body read, and restoring now
            # would replace the qcow2 that xemu has open.
            if _qmp_available():
                self._send_json(409, {
                    "error": "xemu is running; stop the session before restoring a state",
                })
                return
            self._restore_state_file(filename, content)
        finally:
            _release_state_file()

    def _read_state_body(self) -> bytes | None:
        """Read the pushed archive. None once an error response has been sent."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "missing or invalid Content-Length"})
            return None
        if length > STATE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "state file exceeds size limit"})
            return None
        content = self.rfile.read(length)
        if len(content) != length:
            self._send_json(400, {"error": "truncated request body"})
            return None
        return content

    def _restore_state_file(self, filename: str, content: bytes):
        """Swap in the pushed disk image. Caller holds the flag."""
        error = _restore_hdd_image(content)
        if error is not None:
            self._send_json(400, {"error": error})
            return
        log.info("state-file: restored %s (%d bytes)", filename, len(content))
        self._send_json(200, {"status": "ok", "filename": filename})

    def do_DELETE(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path == "/launch":
            # Ends a game session or a setup session. Kill rather than eject to
            # the dashboard: an idle xemu busy-loops several CPU cores under
            # software rendering.
            #
            # Stopping is never refused — it is the only way out of a hung
            # launch — so instead of a 409 the marker below tells a background
            # launch its session is gone before it writes any state back.
            with _lock:
                _state["session_generation"] += 1

            # A /state-file transfer holds the guest paused mid-read and a save
            # is a snapshot job SIGTERM would cut in half; give either a bounded
            # window to finish. The stop still wins if the window runs out.
            deadline = time.monotonic() + STOP_WAIT
            while True:
                with _lock:
                    transferring = _state["state_file_in_progress"]
                    saving = _state["save_in_progress"]
                if not (transferring or saving):
                    break
                if time.monotonic() >= deadline:
                    log.warning(
                        "Stopping xemu with %s still in flight",
                        "a /state-file transfer" if transferring else "a snapshot job",
                    )
                    break
                time.sleep(0.1)

            _end_setup()
            _kill_xemu()
            with _lock:
                _state["rom_path"] = None
                _state["rom_name"] = None
                _state["started_at"] = None
                # Errors describe the session that was just ended explicitly;
                # /status must not keep reporting them afterwards.
                _state["launch_error"] = None
                _state["resume_error"] = None
            log.info("Session ended via DELETE /launch — xemu stopped")
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found"})


# ── Main ──────────────────────────────────────────────────────────────────────


def _graceful_shutdown(server: HTTPServer, signum: int) -> None:
    """Stop the HTTP listener, let any in-flight snapshot finish, kill xemu.
    Triggered on SIGTERM/SIGINT — serve_forever()'s KeyboardInterrupt path
    does not cover SIGTERM from s6/systemd. Killing xemu here prevents the
    broker restart from leaving an orphan burning CPU with no QMP owner."""
    log.info("Received signal %d — beginning graceful shutdown", signum)
    Thread(target=server.shutdown, daemon=True).start()

    wait = max(QMP_WAIT, 5.0)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        with _lock:
            if not _state["save_in_progress"]:
                break
        time.sleep(0.2)
    else:
        log.warning("Shutdown: in-flight snapshot did not conclude within %.1fs", wait)

    _kill_xemu()
    log.info("Shutdown complete")


def main():
    log.info("Broker starting on port %d", PORT)
    if not SECRET:
        log.warning("BROKER_SECRET not set — all POST/DELETE endpoints are unauthenticated")

    # A previous broker run may have left an unmanaged xemu behind.
    _reap_stray_xemu()

    # ThreadingHTTPServer: snapshot loads run inline in the handler (up to
    # QMP_WAIT); a single-threaded server would stall /health and /status.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("xemu broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    def _handle(signum, _frame):
        _graceful_shutdown(server, signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
