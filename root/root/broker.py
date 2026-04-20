#!/usr/bin/env python3
"""broker.py — launch xemu on demand and expose a small HTTP API."""

import glob
import hmac
import json
import logging
import os
import signal
import socket as _socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread, Lock

# ── Config ────────────────────────────────────────────────────────────────────

PORT        = int(os.environ.get("BROKER_PORT", "8000"))
SECRET      = os.environ.get("BROKER_SECRET", "")
ROM_ROOT    = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
QMP_SOCKET  = Path(os.environ.get("QMP_SOCKET", "/tmp/xemu-qmp.sock"))
QMP_TIMEOUT = float(os.environ.get("QMP_TIMEOUT", "2.0"))
QMP_WAIT    = float(os.environ.get("QMP_WAIT", "10.0"))

# ENV passed to xemu via sudo -u abc env.
# DISPLAY=:0       — Xwayland under labwc (pixelflux compositor chain)
# WAYLAND_DISPLAY  — pixelflux creates wayland-1 in XDG_RUNTIME_DIR
# LD_PRELOAD       — joystick interposer redirects /dev/input/* opens to selkies
#                    Unix sockets so controller input reaches xemu.
ENV = {
    "DISPLAY":            ":0",
    "WAYLAND_DISPLAY":    "wayland-1",
    "XDG_RUNTIME_DIR":    "/config/.XDG",
    "PULSE_RUNTIME_PATH": "/defaults",
    "DRI_NODE":           os.environ.get("DRI_NODE", ""),
    "DRINODE":            os.environ.get("DRINODE", ""),
    "HOME":               "/config",
    "USER":               "abc",
    "LD_PRELOAD":         "/usr/lib/selkies_joystick_interposer.so",
}

logging.basicConfig(
    level=getattr(logging, os.environ.get("BROKER_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_session_lock = Lock()
_session: dict = {
    "process":          None,
    "rom_path":         None,
    "rom_name":         None,
    "started_at":       None,
    "is_managed":       False,
    "save_in_progress": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_rom_path(raw: str) -> Path | None:
    """Resolve raw to an absolute path and confirm it lives under ROM_ROOT."""
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    return p


def _kill_xemu() -> None:
    """Kill the managed xemu process group. Releases lock before waiting."""
    with _session_lock:
        _session["is_managed"] = False
        proc = _session["process"]
        _session["process"] = None
        _session["rom_path"] = None
        _session["rom_name"] = None
        _session["started_at"] = None

    if proc is None or proc.poll() is not None:
        log.debug("_kill_xemu: no running process to kill")
        return

    log.info("Stopping xemu (PID %d)...", proc.pid)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        log.debug("_kill_xemu: SIGTERM sent to pgid %d", pgid)
        try:
            proc.wait(timeout=5)
            log.debug("_kill_xemu: process exited cleanly after SIGTERM")
        except subprocess.TimeoutExpired:
            log.warning("xemu did not exit after SIGTERM — sending SIGKILL")
            os.killpg(pgid, signal.SIGKILL)
            proc.wait()
            log.debug("_kill_xemu: process killed with SIGKILL")
    except ProcessLookupError:
        log.debug("_kill_xemu: process already gone")


def _drain_gamepad_sockets() -> None:
    """Send EOF to each selkies gamepad socket before launching a new session.

    Connecting and immediately sending SHUT_WR causes readexactly(1) in phase 1
    to raise IncompleteReadError — the handler exits cleanly before entering
    the phase-2 keep-alive loop. Socket files that refuse connection are stale
    and are unlinked.
    """
    paths = sorted(
        glob.glob("/tmp/selkies_js*.sock") + glob.glob("/tmp/selkies_event*.sock")
    )
    if not paths:
        log.debug("Socket drain: no gamepad sockets found.")
        return

    drained = 0
    removed = 0
    for path in paths:
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(path)
                s.shutdown(_socket.SHUT_WR)
            drained += 1
        except OSError:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass

    log.debug(
        "Socket drain: sent EOF to %d socket(s), removed %d dead file(s) (of %d total).",
        drained, removed, len(paths),
    )


def _log_xemu_output(proc: subprocess.Popen) -> None:
    """Read xemu stdout/stderr line-by-line and emit as [xemu] DEBUG log entries."""
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.debug("[xemu] %s", line)
    except Exception as exc:
        log.debug("_log_xemu_output: reader exited: %s", exc)


def _launch_xemu_internal(rom_path: str | None) -> None:
    """Launch /usr/bin/xemu as abc via sudo+env with QMP socket enabled."""
    cmd = [
        "sudo", "-u", "abc", "env",
        *[f"{k}={v}" for k, v in ENV.items()],
        "xemu",
        "-full-screen",
        "-qmp", f"unix:{QMP_SOCKET},server,nowait",
    ]
    if rom_path:
        cmd.extend(["-dvd_path", rom_path])

    log.info("Launching xemu (rom=%s)", rom_path or "dashboard")
    log.debug("_launch_xemu_internal: cmd=%s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp,
        )
    except Exception as exc:
        log.error("_launch_xemu_internal: failed to launch xemu: %s", exc)
        with _session_lock:
            _session["process"] = None
            _session["is_managed"] = False
        return

    with _session_lock:
        _session["process"] = proc
        _session["is_managed"] = True
    log.info("xemu launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()
    Thread(target=_log_xemu_output, args=(proc,), daemon=True).start()


def _monitor_process(proc: subprocess.Popen, start_time: float) -> None:
    """On unexpected exit, relaunch the dashboard if the session is still managed."""
    proc.wait()
    exit_code = proc.returncode
    duration = time.monotonic() - start_time
    log.debug(
        "_monitor_process: xemu exited (code=%s, duration=%.1fs)",
        exit_code, duration,
    )

    with _session_lock:
        should_relaunch = _session["is_managed"] and _session["process"] is proc

    if not should_relaunch:
        log.debug("_monitor_process: managed=False or proc replaced — not relaunching")
        return

    wait_time = 5 if duration < 5 else 1  # longer delay for quick crashes
    log.info(
        "xemu exited after %.1fs (code=%s) — relaunching dashboard in %ds",
        duration, exit_code, wait_time,
    )
    time.sleep(wait_time)

    with _session_lock:
        if not _session["is_managed"]:
            log.debug("_monitor_process: managed cleared during sleep — aborting relaunch")
            return

    _launch_xemu(None)


def _launch_xemu(rom_path: str | None) -> None:
    """Top-level launch: kill any running xemu, drain sockets, launch fresh."""
    _kill_xemu()
    _drain_gamepad_sockets()
    time.sleep(2)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        _session["started_at"] = started_at
    _launch_xemu_internal(rom_path)


# ── QMP helpers ───────────────────────────────────────────────────────────────

def _qmp_recv_line(sock: _socket.socket) -> dict:
    """Receive one newline-terminated JSON object from a QMP socket."""
    buf = b""
    while True:
        if b"\n" in buf:
            line, _, _ = buf.partition(b"\n")
            return json.loads(line)
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("QMP socket closed unexpectedly")
        buf += chunk


def _qmp_command(cmd: str, args: dict | None = None) -> dict:
    """Open a fresh QMP connection, negotiate capabilities, send one command.

    The connection is closed after the response is received.
    QMP savevm/loadvm block until the operation completes, so QMP_WAIT is used
    as the socket timeout for the command response.

    Raises OSError if the socket is unavailable or the connection fails.
    Raises ValueError if QMP returns an error response.
    """
    payload: dict = {"execute": cmd}
    if args:
        payload["arguments"] = args

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.settimeout(QMP_TIMEOUT)
        sock.connect(str(QMP_SOCKET))

        # 1. Read QMP greeting: {"QMP": {"version": {...}, "capabilities": [...]}}
        _qmp_recv_line(sock)

        # 2. Negotiate capabilities
        sock.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
        _qmp_recv_line(sock)  # {"return": {}}

        # 3. Send command. savevm/loadvm may block for several seconds.
        sock.settimeout(QMP_WAIT)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        response = _qmp_recv_line(sock)
    except (OSError, json.JSONDecodeError) as exc:
        sock.close()
        raise OSError(f"QMP {cmd} failed: {exc}") from exc
    else:
        sock.close()

    if "error" in response:
        desc = response["error"].get("desc", str(response["error"]))
        raise ValueError(f"QMP error: {desc}")
    return response


def _qmp_save_state(slot: int) -> bool:
    """Save game state to broker-slot-N via QMP. Returns True on success."""
    name = f"broker-slot-{slot}"
    try:
        _qmp_command("savevm", {"name": name})
        log.info("QMP: savevm %s complete", name)
        return True
    except (OSError, ValueError) as exc:
        log.error("QMP: savevm %s failed: %s", name, exc)
        return False


def _qmp_load_state(slot: int) -> bool:
    """Load game state from broker-slot-N via QMP. Returns True on success."""
    name = f"broker-slot-{slot}"
    try:
        _qmp_command("loadvm", {"name": name})
        log.info("QMP: loadvm %s complete", name)
        return True
    except (OSError, ValueError) as exc:
        log.error("QMP: loadvm %s failed: %s", name, exc)
        return False


# ── PulseAudio helpers ────────────────────────────────────────────────────────

_PACTL_CMD = [
    "sudo", "-u", "abc", "env",
    "PULSE_RUNTIME_PATH=/defaults",
    "HOME=/config",
    "USER=abc",
]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl as abc so it connects to abc's PulseAudio instance."""
    cmd = _PACTL_CMD + ["pactl"] + list(args)
    log.debug("_pactl: cmd=%s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5)


def _pactl_get_mute() -> bool | None:
    """Return current mute state as bool, or None on error."""
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
        log.error("_pactl_get_mute: pactl failed: %s", result.stderr.strip())
        return None
    return result.stdout.strip().endswith("yes")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BrokerHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _check_secret(self) -> bool:
        if not SECRET:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Broker-Secret", ""),
            SECRET,
        )

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)
        log.debug("HTTP response: %d %s", code, body)

    def _read_body(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
        except ValueError:
            length = 0
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        log.debug("HTTP GET %s", self.path)
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/status":
            with _session_lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                    and _session["rom_path"] is not None
                )
                rom_path   = _session["rom_path"]   if active else None
                rom_name   = _session["rom_name"]   if active else None
                started_at = _session["started_at"] if active else None
            self._send_json(200, {
                "active":     active,
                "rom_path":   rom_path,
                "rom_name":   rom_name,
                "started_at": started_at,
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        log.debug("HTTP POST %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/save-and-exit":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            wait = body.get("wait", True)
            if wait:
                try:
                    ok = _qmp_save_state(10)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("save-and-exit: QMP save failed — exiting anyway")
                _kill_xemu()
                self._send_json(200, {"status": "ok", "saved": ok})
                Thread(target=_launch_xemu, args=(None,), daemon=True).start()
            else:
                def _bg():
                    try:
                        ok = _qmp_save_state(10)
                    finally:
                        with _session_lock:
                            _session["save_in_progress"] = False
                    if not ok:
                        log.warning("save-and-exit: QMP save failed — exiting anyway")
                    _kill_xemu()
                    _launch_xemu(None)
                Thread(target=_bg, daemon=True).start()
                self._send_json(200, {"status": "queued", "saved": False})
            return

        if self.path == "/save-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 9):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–9"})
                return
            def _bg_save(s):
                try:
                    ok = _qmp_save_state(s)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("save-state: QMP save failed for slot %d", s)
            Thread(target=_bg_save, args=(slot,), daemon=True).start()
            self._send_json(200, {"status": "saving", "slot": slot})
            return

        if self.path == "/load-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save in progress"})
                    return
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 9):
                self._send_json(400, {"error": "slot must be 1–9"})
                return
            ok = _qmp_load_state(slot)
            self._send_json(
                200 if ok else 503,
                {"status": "ok" if ok else "error", "loaded": ok, "slot": slot},
            )
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
            log.info("Volume set to %d%%", level)
            self._send_json(200, {"status": "ok", "level": level})
            return

        if self.path == "/mute":
            body = self._read_body()
            if "mute" in body:
                mute_arg = "1" if body["mute"] else "0"
            else:
                mute_arg = "toggle"
            result = _pactl("set-sink-mute", "@DEFAULT_SINK@", mute_arg)
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            mute_state = _pactl_get_mute()
            log.info("Mute %s", "on" if mute_state else "off")
            self._send_json(200, {"status": "ok", "mute": mute_state})
            return

        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

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

        Thread(target=_launch_xemu, args=(str(rom_path),), daemon=True).start()
        self._send_json(200, {"status": "launching", "rom_path": str(rom_path)})

    def do_DELETE(self):
        log.debug("HTTP DELETE %s", self.path)
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        Thread(target=_launch_xemu, args=(None,), daemon=True).start()
        log.info("Soft reset: returning to dashboard")
        self._send_json(200, {"status": "resetting"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Broker-Secret")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Broker starting — waiting 5s for desktop to initialise...")
    if not SECRET:
        log.warning("BROKER_SECRET is not set — all POST/DELETE endpoints are unauthenticated")

    log.debug("Startup ENV: %s", {k: ("***" if k == "BROKER_SECRET" else v) for k, v in ENV.items()})

    time.sleep(5)

    # Kill any stale xemu from a previous broker run (SIGTERM first, then SIGKILL).
    result = subprocess.run(["pkill", "-15", "-x", "xemu"], capture_output=True)
    if result.returncode == 0:
        log.info("Sent SIGTERM to stale xemu instance(s) on startup.")
        time.sleep(3)
        subprocess.run(["pkill", "-9", "-x", "xemu"], capture_output=True)
        time.sleep(1)

    # Auto-launch xemu so the stream shows something while no game is running.
    Thread(target=_launch_xemu, args=(None,), daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("xemu broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
