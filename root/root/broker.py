#!/usr/bin/env python3
"""broker.py — QMP shim for xemu ROM injection and save-state management."""

import hmac
import json
import logging
import os
import socket as _socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread, Lock

# ── Config ────────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("BROKER_PORT", "8000"))
SECRET = os.environ.get("BROKER_SECRET", "")
ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
QMP_SOCKET = Path(os.environ.get("QMP_SOCKET", "/tmp/xemu-qmp.sock"))
QMP_TIMEOUT = float(os.environ.get("QMP_TIMEOUT", "2.0"))
QMP_WAIT = float(os.environ.get("QMP_WAIT", "10.0"))
QMP_BOOT_TIMEOUT = float(os.environ.get("QMP_BOOT_TIMEOUT", "60.0"))

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
    "rom_path": None,
    "rom_name": None,
    "started_at": None,
    "save_in_progress": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _validate_rom_path(raw: str) -> Path | None:
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    return p


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


def _qmp_load_rom(rom_path: str) -> bool:
    try:
        _qmp_command("blockdev-change-medium", {"device": "ide0-cd1", "filename": rom_path})
    except (OSError, ValueError) as exc:
        log.error("QMP: blockdev-change-medium failed: %s", exc)
        return False
    try:
        _qmp_command("system_reset")
        log.info("QMP: loaded ROM %s and reset console", rom_path)
        return True
    except (OSError, ValueError) as exc:
        log.error("QMP: system_reset failed: %s", exc)
        return False


def _qmp_return_to_dashboard() -> bool:
    """Eject the disc and reset — xemu boots back to the Xbox dashboard."""
    try:
        _qmp_command("eject", {"device": "ide0-cd1"})
    except (OSError, ValueError):
        pass
    try:
        _qmp_command("system_reset")
        log.info("QMP: disc ejected and console reset to dashboard")
        return True
    except (OSError, ValueError) as exc:
        log.error("QMP: system_reset failed: %s", exc)
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
        while True:
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

        # Wait for the job to conclude
        deadline = time.monotonic() + QMP_WAIT
        concluded = False
        while time.monotonic() < deadline:
            msg = recv_msg()
            if (msg.get("event") == "JOB_STATUS_CHANGE"
                    and msg.get("data", {}).get("id") == job_id
                    and msg.get("data", {}).get("status") == "concluded"):
                concluded = True
                break

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


def _qmp_quit() -> None:
    try:
        _qmp_command("quit")
    except (OSError, ValueError):
        pass


# ── ROM loading (background) ──────────────────────────────────────────────────


def _do_load_rom(rom_path: str) -> None:
    """Wait for QMP, inject ROM, update state. Runs in a background thread."""
    log.info("Waiting for xemu QMP (up to %.0fs)...", QMP_BOOT_TIMEOUT)
    if not _qmp_wait_ready(QMP_BOOT_TIMEOUT):
        log.error("QMP not available after %.0fs — ROM load aborted", QMP_BOOT_TIMEOUT)
        return

    ok = _qmp_load_rom(rom_path)
    with _lock:
        if ok:
            _state["rom_path"] = rom_path
            _state["rom_name"] = Path(rom_path).stem
            _state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── PulseAudio ────────────────────────────────────────────────────────────────

_PACTL_CMD = [
    "sudo", "-u", "abc",
    "env", "PULSE_RUNTIME_PATH=/defaults", "HOME=/config", "USER=abc",
]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        _PACTL_CMD + ["pactl"] + list(args),
        capture_output=True, text=True, timeout=5,
    )


def _pactl_get_mute() -> bool | None:
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
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
            self.headers.get("X-Broker-Secret", ""), SECRET
        )

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Broker-Secret")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/status":
            xemu_up = _qmp_available()
            with _lock:
                rom_path = _state["rom_path"]
                rom_name = _state["rom_name"]
                started_at = _state["started_at"]
            self._send_json(200, {
                "xemu_running": xemu_up,
                "active": xemu_up and rom_path is not None,
                "rom_path": rom_path,
                "rom_name": rom_name,
                "started_at": started_at,
            })
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
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
            Thread(target=_do_load_rom, args=(str(rom_path),), daemon=True).start()
            self._send_json(200, {"status": "loading", "rom_path": str(rom_path)})
            return

        if self.path == "/save-state":
            with _lock:
                if _state["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _state["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _state["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 10):
                with _lock:
                    _state["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–10"})
                return

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
            with _lock:
                if _state["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _state["save_in_progress"]:
                    self._send_json(409, {"error": "save in progress"})
                    return
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 10):
                self._send_json(400, {"error": "slot must be 1–10"})
                return
            ok = _qmp_load_state(slot)
            self._send_json(
                200 if ok else 503,
                {"status": "ok" if ok else "error", "loaded": ok, "slot": slot},
            )
            return

        if self.path == "/save-and-exit":
            with _lock:
                if _state["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _state["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _state["save_in_progress"] = True

            def _bg_exit():
                try:
                    ok = _qmp_save_state(10)
                    if not ok:
                        log.warning("save-and-exit: save failed — returning to dashboard anyway")
                    _qmp_return_to_dashboard()
                finally:
                    with _lock:
                        _state["save_in_progress"] = False
                        _state["rom_path"] = None
                        _state["rom_name"] = None
                        _state["started_at"] = None

            Thread(target=_bg_exit, daemon=True).start()
            self._send_json(200, {"status": "queued"})
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
                mute_arg = "1" if body["mute"] else "0"
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

    def do_DELETE(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path == "/launch":
            _qmp_return_to_dashboard()
            with _lock:
                _state["rom_path"] = None
                _state["rom_name"] = None
                _state["started_at"] = None
            log.info("Disc ejected and returned to dashboard via DELETE /launch")
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found"})


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    log.info("Broker starting on port %d", PORT)
    if not SECRET:
        log.warning("BROKER_SECRET not set — all POST/DELETE endpoints are unauthenticated")

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
