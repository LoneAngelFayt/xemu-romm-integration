#!/usr/bin/env python3
"""broker.py — launch xemu on demand and manage it over QMP.

The broker owns the xemu process lifecycle: it spawns xemu (with the QMP
socket flag) when a ROM is launched and kills it when the session ends.
xemu is QEMU-based and busy-loops several CPU cores while idling at the
dashboard, so no gameless instance is ever kept around."""

import base64
import binascii
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
import urllib.request
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
# The archive travels compressed but lands on disk expanded, so the two need
# separate bounds. Holding the image to the transfer limit rejected real saves:
# a qcow2 carrying one ~70MB VM state runs past 256MB while zipping to under 50,
# and the qcow2 never shrinks when a snapshot is deleted, so it only creeps up.
HDD_IMAGE_MAX_BYTES = int(os.environ.get("HDD_IMAGE_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
STATE_GET_WAIT = float(os.environ.get("STATE_GET_WAIT", "30.0"))

# A state archive carries only the snapshot it is for. xemu keeps every slot in
# one qcow2 and cannot export a snapshot on its own, so without this slot 5
# ships slots 1-4 inside it and archives grow with every save. Set to 0 to serve
# the whole image, which is also where every failure in the rebuild lands.
STATE_TRIM = os.environ.get("STATE_TRIM", "1").strip().lower() not in {"0", "false", "no"}

# Captured state frames sit beside the disk image so they outlive xemu: RomM
# asks for the frame during the pull, by which time /save-and-exit has already
# killed the process that could have drawn it.
STATE_SHOT_DIR = Path(os.environ.get("STATE_SHOT_DIR", str(HDD_IMAGE.parent)))

# Port of the pixelflux Computer Use server that frames are captured from, set
# by init.sh. Empty means no capture server, and states then carry no thumbnail.
CU_PORT = os.environ.get("PIXELFLUX_CU", "").strip()
CU_TIMEOUT = float(os.environ.get("STATE_SHOT_TIMEOUT", "10.0"))

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


# ── Trimming a state archive to one snapshot ──────────────────────────────────
#
# The archive used to be the whole disk image, which meant slot 5 shipped every
# earlier slot's VM state inside it and archives grew with each save. Rebuilding
# the image around the one snapshot being served leaves the others behind, and
# leaves behind clusters an interrupted snapshot job leaked as well, because the
# rebuild copies only what the two surviving mappings actually reach.

_QCOW2_OFFSET_MASK = 0x00FFFFFFFFFFFE00
_QCOW2_FLAG_COPIED = 1 << 63
_QCOW2_FLAG_COMPRESSED = 1 << 62


class _Qcow2Error(Exception):
    """This image is not one the rebuild is prepared to touch.

    Always caught: the caller then serves the untouched image, so refusing here
    costs archive size and never a save."""


def _ceil_div(n: int, by: int) -> int:
    return -(-n // by)


def _qcow2_read_at(fh, offset: int, size: int) -> bytes:
    """Read exactly `size` bytes; a short read means a truncated image."""
    if offset < 0 or size < 0:
        raise _Qcow2Error(f"nonsense read of {size} bytes at {offset}")
    fh.seek(offset)
    data = fh.read(size)
    if len(data) != size:
        raise _Qcow2Error(f"image ends inside a {size}-byte read at {offset}")
    return data


def _qcow2_parse_header(fh) -> dict:
    head = _qcow2_read_at(fh, 0, 72)
    if head[:4] != _QCOW2_MAGIC:
        raise _Qcow2Error("not a qcow2")
    version = int.from_bytes(head[4:8], "big")
    if version not in (2, 3):
        raise _Qcow2Error(f"qcow2 version {version}")
    if int.from_bytes(head[8:16], "big"):
        raise _Qcow2Error("backing files are not supported")
    cluster_bits = int.from_bytes(head[20:24], "big")
    if not 9 <= cluster_bits <= 21:
        raise _Qcow2Error(f"implausible cluster_bits {cluster_bits}")
    if int.from_bytes(head[32:36], "big"):
        raise _Qcow2Error("encrypted images are not supported")
    refcount_order = 4
    if version >= 3:
        # An external data file, extended L2 entries, a non-zlib compression
        # type or a corrupt marker all change what the tables below mean, so
        # none of them are guessed at.
        if int.from_bytes(_qcow2_read_at(fh, 72, 8), "big"):
            raise _Qcow2Error("image sets incompatible feature bits")
        refcount_order = int.from_bytes(_qcow2_read_at(fh, 96, 4), "big")
    if refcount_order != 4:
        raise _Qcow2Error(f"refcount_order {refcount_order} is not supported")
    return {
        "version": version,
        "cluster_bits": cluster_bits,
        "cluster": 1 << cluster_bits,
        "size": int.from_bytes(head[24:32], "big"),
        "l1_size": int.from_bytes(head[36:40], "big"),
        "l1_offset": int.from_bytes(head[40:48], "big"),
        "rc_offset": int.from_bytes(head[48:56], "big"),
        "rc_clusters": int.from_bytes(head[56:60], "big"),
        "nb_snapshots": int.from_bytes(head[60:64], "big"),
        "snapshots_offset": int.from_bytes(head[64:72], "big"),
    }


def _qcow2_read_snapshot_table(fh, hdr: dict) -> list:
    """Every snapshot entry, keeping raw bytes so one can be written back."""
    out = []
    pos = hdr["snapshots_offset"]
    for _ in range(hdr["nb_snapshots"]):
        entry = _qcow2_read_at(fh, pos, 40)
        (l1_offset, l1_size, id_len, name_len, _sec, _nsec, _clock,
         vm_state_size, extra_len) = struct.unpack(_QCOW2_SNAPSHOT_ENTRY, entry)
        body = _qcow2_read_at(fh, pos + 40, extra_len + id_len + name_len)
        pad = -len(body) % 8
        _qcow2_read_at(fh, pos + 40 + len(body), pad)
        out.append({
            "name": body[extra_len + id_len:].decode("utf-8", "replace"),
            "l1_offset": l1_offset,
            "l1_size": l1_size,
            "vm_state_size": vm_state_size,
            "raw": entry + body + b"\x00" * pad,
        })
        pos += 40 + len(body) + pad
    return out


def _qcow2_read_l1(fh, offset: int, size: int) -> tuple:
    if not size:
        return ()
    return struct.unpack(f">{size}Q", _qcow2_read_at(fh, offset, size * 8))


def _qcow2_mapping(fh, hdr: dict, l1_offset: int, l1_size: int) -> dict:
    """Guest cluster index to host offset, for every allocated cluster."""
    cluster = hdr["cluster"]
    per_l2 = cluster // 8
    mapping = {}
    for i, entry in enumerate(_qcow2_read_l1(fh, l1_offset, l1_size)):
        l2_offset = entry & _QCOW2_OFFSET_MASK
        if not l2_offset:
            continue
        l2 = struct.unpack(f">{per_l2}Q", _qcow2_read_at(fh, l2_offset, cluster))
        for j, l2e in enumerate(l2):
            if l2e & _QCOW2_FLAG_COMPRESSED:
                raise _Qcow2Error("compressed clusters are not supported")
            host = l2e & _QCOW2_OFFSET_MASK
            if host:
                mapping[i * per_l2 + j] = host
    return mapping


def _qcow2_rebuild(src: Path, dst: Path, keep: str) -> dict:
    """Write dst holding the active disk and only the snapshot named `keep`.

    Sharing is preserved: an L2 table or data cluster both mappings point at is
    written once and pointed at twice, so a snapshot that has barely diverged
    from the live disk stays as cheap as it is in the source image."""
    with src.open("rb") as fh:
        hdr = _qcow2_parse_header(fh)
        cluster = hdr["cluster"]
        per_l2 = cluster // 8

        snapshots = _qcow2_read_snapshot_table(fh, hdr)
        matches = [s for s in snapshots if s["name"] == keep]
        if len(matches) != 1:
            raise _Qcow2Error(
                f"{keep!r} names {len(matches)} snapshots, expected exactly one")
        kept = matches[0]

        active_l1 = _qcow2_read_l1(fh, hdr["l1_offset"], hdr["l1_size"])
        snap_l1 = _qcow2_read_l1(fh, kept["l1_offset"], kept["l1_size"])

        l2_order, l2_at = [], {}
        for entries in (active_l1, snap_l1):
            for entry in entries:
                offset = entry & _QCOW2_OFFSET_MASK
                if offset and offset not in l2_at:
                    l2_at[offset] = len(l2_order)
                    l2_order.append(offset)

        l2_body, data_order, data_at = {}, [], {}
        for offset in l2_order:
            entries = struct.unpack(f">{per_l2}Q", _qcow2_read_at(fh, offset, cluster))
            l2_body[offset] = entries
            for l2e in entries:
                if l2e & _QCOW2_FLAG_COMPRESSED:
                    raise _Qcow2Error("compressed clusters are not supported")
                host = l2e & _QCOW2_OFFSET_MASK
                if host and host not in data_at:
                    data_at[host] = len(data_order)
                    data_order.append(host)

        # An L2 table counts once per L1 entry reaching it, and a data cluster
        # once per L2 entry times the refcount of the table that entry sits in.
        # That last factor is how QEMU counts a snapshot sharing a table with
        # the active disk, and getting it wrong makes QEMU write in place over a
        # cluster the snapshot still needs.
        l2_refs = dict.fromkeys(l2_order, 0)
        for entries in (active_l1, snap_l1):
            for entry in entries:
                offset = entry & _QCOW2_OFFSET_MASK
                if offset:
                    l2_refs[offset] += 1
        data_refs = dict.fromkeys(data_order, 0)
        for offset in l2_order:
            for l2e in l2_body[offset]:
                host = l2e & _QCOW2_OFFSET_MASK
                if host:
                    data_refs[host] += l2_refs[offset]

        l1a_n = _ceil_div(hdr["l1_size"] * 8, cluster)
        l1s_n = _ceil_div(kept["l1_size"] * 8, cluster)
        snap_n = _ceil_div(len(kept["raw"]), cluster)
        fixed = 1 + l1a_n + l1s_n + snap_n + len(l2_order) + len(data_order)
        # The refcount structures have to cover a total that includes them, so
        # size them against their own growth until it settles.
        per_block = cluster // 2
        blocks = table_n = 1
        for _ in range(16):
            total = fixed + blocks + table_n
            want_blocks = max(1, _ceil_div(total, per_block))
            want_table = max(1, _ceil_div(want_blocks * 8, cluster))
            if (want_blocks, want_table) == (blocks, table_n):
                break
            blocks, table_n = want_blocks, want_table
        else:
            raise _Qcow2Error("refcount table sizing did not settle")
        total = fixed + blocks + table_n

        table_c = 1
        block_c = table_c + table_n
        l1a_c = block_c + blocks
        l1s_c = l1a_c + l1a_n
        snap_c = l1s_c + l1s_n
        l2_c = snap_c + snap_n
        data_c = l2_c + len(l2_order)

        counts = [0] * total
        for start, count in ((0, 1), (table_c, table_n), (block_c, blocks),
                             (l1a_c, l1a_n), (l1s_c, l1s_n), (snap_c, snap_n)):
            for i in range(start, start + count):
                counts[i] = 1
        for offset, i in l2_at.items():
            counts[l2_c + i] = l2_refs[offset]
        for offset, i in data_at.items():
            counts[data_c + i] = data_refs[offset]

        def moved_l1(entries, mark_copied):
            moved = []
            for entry in entries:
                offset = entry & _QCOW2_OFFSET_MASK
                if not offset:
                    moved.append(0)
                    continue
                flag = (_QCOW2_FLAG_COPIED
                        if mark_copied and l2_refs[offset] == 1 else 0)
                moved.append((l2_c + l2_at[offset]) * cluster | flag)
            return struct.pack(f">{len(moved)}Q", *moved) if moved else b""

        def moved_l2(entries):
            moved = []
            for l2e in entries:
                host = l2e & _QCOW2_OFFSET_MASK
                if not host:
                    # In v3 bit 0 marks a cluster that reads as zeroes; v2 has
                    # no such flag and every other bit here is reserved.
                    moved.append(l2e & 1 if hdr["version"] >= 3 else 0)
                    continue
                flag = _QCOW2_FLAG_COPIED if data_refs[host] == 1 else 0
                moved.append((data_c + data_at[host]) * cluster | flag)
            return struct.pack(f">{per_l2}Q", *moved)

        with dst.open("wb") as out:
            out.truncate(total * cluster)

            header = bytearray(cluster)
            header[0:4] = _QCOW2_MAGIC
            header[4:8] = hdr["version"].to_bytes(4, "big")
            header[20:24] = hdr["cluster_bits"].to_bytes(4, "big")
            header[24:32] = hdr["size"].to_bytes(8, "big")
            header[36:40] = hdr["l1_size"].to_bytes(4, "big")
            header[40:48] = (l1a_c * cluster).to_bytes(8, "big")
            header[48:56] = (table_c * cluster).to_bytes(8, "big")
            header[56:60] = table_n.to_bytes(4, "big")
            header[60:64] = (1).to_bytes(4, "big")
            header[64:72] = (snap_c * cluster).to_bytes(8, "big")
            if hdr["version"] >= 3:
                header[96:100] = (4).to_bytes(4, "big")
                header[100:104] = (104).to_bytes(4, "big")
            out.seek(0)
            out.write(header)

            refcount_table = [0] * (table_n * cluster // 8)
            for i in range(blocks):
                refcount_table[i] = (block_c + i) * cluster
            out.seek(table_c * cluster)
            out.write(struct.pack(f">{len(refcount_table)}Q", *refcount_table))

            for i in range(blocks):
                span = counts[i * per_block:(i + 1) * per_block]
                block = bytearray(cluster)
                if span:
                    struct.pack_into(f">{len(span)}H", block, 0, *span)
                out.seek((block_c + i) * cluster)
                out.write(block)

            out.seek(l1a_c * cluster)
            out.write(moved_l1(active_l1, True))
            # Snapshot L1 entries never carry COPIED: a snapshot is read-only,
            # so nothing may ever write over one of its clusters in place.
            out.seek(l1s_c * cluster)
            out.write(moved_l1(snap_l1, False))

            entry = bytearray(kept["raw"])
            entry[0:8] = (l1s_c * cluster).to_bytes(8, "big")
            out.seek(snap_c * cluster)
            out.write(entry)

            for offset in l2_order:
                out.seek((l2_c + l2_at[offset]) * cluster)
                out.write(moved_l2(l2_body[offset]))
            for offset in data_order:
                out.seek((data_c + data_at[offset]) * cluster)
                out.write(_qcow2_read_at(fh, offset, cluster))

            out.flush()
            os.fsync(out.fileno())

    return {"clusters": total, "dropped": len(snapshots) - 1}


def _qcow2_check_refcounts(fh, hdr: dict) -> None:
    """Every stored refcount must match what the tables actually reference."""
    cluster = hdr["cluster"]
    per_l2 = cluster // 8
    per_block = cluster // 2
    total = _ceil_div(fh.seek(0, os.SEEK_END), cluster)
    expected = [0] * total

    def bump(offset):
        index = offset // cluster
        if index >= total:
            raise _Qcow2Error(f"reference to cluster {index} past the image end")
        expected[index] += 1

    bump(0)
    for i in range(hdr["rc_clusters"]):
        bump(hdr["rc_offset"] + i * cluster)
    table = struct.unpack(
        f">{hdr['rc_clusters'] * cluster // 8}Q",
        _qcow2_read_at(fh, hdr["rc_offset"], hdr["rc_clusters"] * cluster),
    )
    for entry in table:
        if entry & _QCOW2_OFFSET_MASK:
            bump(entry & _QCOW2_OFFSET_MASK)

    snapshots = _qcow2_read_snapshot_table(fh, hdr)
    for i in range(_ceil_div(sum(len(s["raw"]) for s in snapshots), cluster)):
        bump(hdr["snapshots_offset"] + i * cluster)

    tables = [(hdr["l1_offset"], hdr["l1_size"])]
    tables += [(s["l1_offset"], s["l1_size"]) for s in snapshots]
    for l1_offset, l1_size in tables:
        for i in range(_ceil_div(l1_size * 8, cluster)):
            bump(l1_offset + i * cluster)
        for entry in _qcow2_read_l1(fh, l1_offset, l1_size):
            l2_offset = entry & _QCOW2_OFFSET_MASK
            if not l2_offset:
                continue
            bump(l2_offset)
            for l2e in struct.unpack(
                f">{per_l2}Q", _qcow2_read_at(fh, l2_offset, cluster)
            ):
                if l2e & _QCOW2_OFFSET_MASK:
                    bump(l2e & _QCOW2_OFFSET_MASK)

    stored = [0] * total
    for ti, entry in enumerate(table):
        offset = entry & _QCOW2_OFFSET_MASK
        if not offset:
            continue
        raw = _qcow2_read_at(fh, offset, cluster)
        base = ti * per_block
        n = min(per_block, max(0, total - base))
        if n:
            stored[base:base + n] = struct.unpack(f">{n}H", raw[:n * 2])
    if stored != expected:
        bad = next(i for i in range(total) if stored[i] != expected[i])
        raise _Qcow2Error(
            f"cluster {bad} stores refcount {stored[bad]} but the tables "
            f"reference it {expected[bad]} times")


def _qcow2_verify(src: Path, dst: Path, keep: str) -> None:
    """Prove the rebuilt image still holds everything the archive has to carry.

    Compares the guest-visible bytes of both surviving mappings — the active
    disk, and the kept snapshot, whose mapping is where the VM state lives — and
    rechecks the refcounts the rebuild wrote. Anything short of an exact match
    raises and the caller falls back to the original image."""
    with src.open("rb") as a, dst.open("rb") as b:
        src_hdr = _qcow2_parse_header(a)
        dst_hdr = _qcow2_parse_header(b)
        for field in ("version", "cluster_bits", "size", "l1_size"):
            if src_hdr[field] != dst_hdr[field]:
                raise _Qcow2Error(f"the rebuild changed {field}")
        if dst_hdr["nb_snapshots"] != 1:
            raise _Qcow2Error(f"rebuilt image holds {dst_hdr['nb_snapshots']} snapshots")

        src_snap = [s for s in _qcow2_read_snapshot_table(a, src_hdr)
                    if s["name"] == keep][0]
        dst_snap = _qcow2_read_snapshot_table(b, dst_hdr)[0]
        if dst_snap["name"] != keep:
            raise _Qcow2Error(f"rebuilt image kept {dst_snap['name']!r}, not {keep!r}")
        for field in ("l1_size", "vm_state_size"):
            if src_snap[field] != dst_snap[field]:
                raise _Qcow2Error(f"the rebuild changed the snapshot's {field}")

        cluster = src_hdr["cluster"]
        for src_l1, dst_l1 in (
            ((src_hdr["l1_offset"], src_hdr["l1_size"]),
             (dst_hdr["l1_offset"], dst_hdr["l1_size"])),
            ((src_snap["l1_offset"], src_snap["l1_size"]),
             (dst_snap["l1_offset"], dst_snap["l1_size"])),
        ):
            want = _qcow2_mapping(a, src_hdr, *src_l1)
            got = _qcow2_mapping(b, dst_hdr, *dst_l1)
            if want.keys() != got.keys():
                raise _Qcow2Error(
                    f"the rebuild maps {len(got)} clusters where the original "
                    f"maps {len(want)}")
            for guest, host in want.items():
                if (_qcow2_read_at(a, host, cluster)
                        != _qcow2_read_at(b, got[guest], cluster)):
                    raise _Qcow2Error(f"guest cluster {guest} differs after the rebuild")

        _qcow2_check_refcounts(b, dst_hdr)


def _trim_hdd_image(keep_tag: str) -> Path | None:
    """A copy of the disk image holding only `keep_tag`, or None to skip it.

    None whenever the rebuild or the check after it is anything but happy, and
    the caller then serves the untouched image: a bigger archive is a far better
    outcome than a subtly wrong one."""
    trimmed = HDD_IMAGE.parent / f".{HDD_IMAGE.name}.trim"
    try:
        before = HDD_IMAGE.stat().st_size
        stats = _qcow2_rebuild(HDD_IMAGE, trimmed, keep_tag)
        _qcow2_verify(HDD_IMAGE, trimmed, keep_tag)
        after = trimmed.stat().st_size
    except (_Qcow2Error, OSError, ValueError, struct.error) as exc:
        log.warning("state-file: serving the whole image, %s could not be "
                    "trimmed to %s: %s", HDD_IMAGE, keep_tag, exc)
        trimmed.unlink(missing_ok=True)
        return None
    log.info("state-file: trimmed to %s — %.1f MB from %.1f MB, %d other "
             "snapshot(s) dropped", keep_tag, after / 2 ** 20, before / 2 ** 20,
             stats["dropped"])
    return trimmed


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


def _capture_frame_png() -> bytes | None:
    """Ask pixelflux for a PNG of what is on screen right now.

    In Wayland mode pixelflux is the compositor and implements no screencopy
    protocol, so its Computer Use server is the only frame source inside the
    container: QMP screendump needs pixman xemu does not build, and xwd and grim
    both come up empty. What comes back is the composited output — the picture
    the player is actually looking at — so it does not suffer xemu #774, where
    screendump returns a stale Xbox logo because nv2a never populates the
    DisplaySurface it reads.

    The port is container-internal and must stay unpublished: the API carries no
    credential and injects keyboard and mouse as well as capturing frames."""
    if not CU_PORT:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{CU_PORT}/computer-use",
        data=json.dumps({"action": "screenshot"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CU_TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        # Older base images ship a pixelflux that ignores PIXELFLUX_CU, so a
        # refused connection here means "no capture server", not a failure.
        log.warning("state-shot: pixelflux capture failed: %s", exc)
        return None
    encoded = payload.get("data") if isinstance(payload, dict) else None
    if not encoded:
        log.warning("state-shot: pixelflux returned no image data")
        return None
    try:
        png = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        log.warning("state-shot: pixelflux image was not valid base64: %s", exc)
        return None
    # RomM drops anything that is not a PNG, so a wrong format is caught here
    # rather than becoming a broken thumbnail after the upload.
    if not png.startswith(_PNG_MAGIC):
        log.warning("state-shot: pixelflux image is not a PNG")
        return None
    return png


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
    png = _capture_frame_png()
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


def _zip_hdd_image(keep_tag: str | None = None) -> bytes | None:
    """Zip the hard disk image into memory.

    With xemu up the caller must pause the guest first: this is a qcow2 a live
    QEMU holds open, and a copy taken mid-write can catch torn metadata. With
    xemu gone the image is quiescent and no pause is possible or needed.

    `keep_tag` names the snapshot the archive is for, and the image is rebuilt
    around it so the other slots do not travel too. The whole image is zipped
    whenever that rebuild is skipped or refused."""
    trimmed = _trim_hdd_image(keep_tag) if keep_tag and STATE_TRIM else None
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(trimmed or HDD_IMAGE, HDD_IMAGE_ENTRY)
    except OSError as exc:
        log.error("state-file: could not zip %s: %s", HDD_IMAGE, exc)
        return None
    finally:
        if trimmed:
            trimmed.unlink(missing_ok=True)
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
        if members[0].file_size > HDD_IMAGE_MAX_BYTES:
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

        # An image too big to ever be restored is refused before a compressed
        # copy of it is built. This is the expanded bound, not the transfer one:
        # what comes back is checked against STATE_FILE_MAX_BYTES once zipped.
        try:
            image_size = HDD_IMAGE.stat().st_size
        except OSError as exc:
            log.error("state-file: could not stat %s: %s", HDD_IMAGE, exc)
            self._send_json(500, {"error": "could not read the hard disk image"})
            return
        if image_size > HDD_IMAGE_MAX_BYTES:
            log.error("state-file: %s is %d bytes — over the limit, not zipping",
                      HDD_IMAGE, image_size)
            self._send_json(413, {"error": "state file exceeds size limit"})
            return

        with _lock:
            rom_name = _state["rom_name"]

        still_paused = False
        keep_tag = f"broker-slot-{slot}"
        if not live:
            content = _zip_hdd_image(keep_tag)
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
                content = _zip_hdd_image(keep_tag)
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
