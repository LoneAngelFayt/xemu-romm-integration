"""Tests for the xemu broker, focused on the /setup session lifecycle.

The broker is a single stdlib module under root/root; import it directly and
exercise the state machine plus the HTTP contract against a real (but xemu-less)
server, with the process/QMP helpers mocked out.
"""

import base64
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "root" / "root"))
import broker  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts clean and leaves no armed timer behind."""
    broker._cancel_setup_watchdog()
    with broker._lock:
        broker._state.update({
            "process": None,
            "rom_path": None,
            "rom_name": None,
            "started_at": None,
            "save_in_progress": False,
            "launch_in_progress": False,
            "state_file_in_progress": False,
            "state_file_reading": False,
            "launch_error": None,
            "resume_error": None,
            "setup": False,
            "setup_timer": None,
            "session_generation": 0,
        })
    yield
    broker._cancel_setup_watchdog()


# ── State machine ─────────────────────────────────────────────────────────────


def test_arm_then_cancel_watchdog():
    broker._arm_setup_watchdog()
    with broker._lock:
        assert broker._state["setup_timer"] is not None
    broker._cancel_setup_watchdog()
    with broker._lock:
        assert broker._state["setup_timer"] is None


def test_setup_expired_noops_when_rom_active(monkeypatch):
    calls = []
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["setup"] = True
        broker._state["rom_path"] = "/romm/x.iso"
    broker._setup_expired()
    assert calls == []
    with broker._lock:
        assert broker._state["setup"] is True  # a ROM owns the session now


def test_setup_expired_stops_live_setup(monkeypatch):
    calls = []
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["setup"] = True
        broker._state["rom_path"] = None
    broker._setup_expired()
    assert calls == ["kill"]
    with broker._lock:
        assert broker._state["setup"] is False


def test_setup_expired_clears_started_at(monkeypatch):
    """The session is gone — /status must not report a start time for it."""
    monkeypatch.setattr(broker, "_kill_xemu", lambda: None)
    with broker._lock:
        broker._state["setup"] = True
        broker._state["rom_path"] = None
        broker._state["started_at"] = "2026-07-24T10:00:00Z"
    broker._setup_expired()
    with broker._lock:
        assert broker._state["started_at"] is None


def test_end_setup_clears_flag_and_cancels_timer():
    broker._arm_setup_watchdog()
    with broker._lock:
        broker._state["setup"] = True
    broker._end_setup()
    with broker._lock:
        assert broker._state["setup"] is False
        assert broker._state["setup_timer"] is None


def test_do_setup_happy_arms_watchdog(monkeypatch):
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda rom=None: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    with broker._lock:
        broker._state["setup"] = True
        broker._state["launch_in_progress"] = True
    broker._do_setup()
    with broker._lock:
        assert broker._state["setup"] is True
        assert broker._state["launch_in_progress"] is False
        assert broker._state["setup_timer"] is not None
        assert broker._state["started_at"] is not None


def test_do_setup_launch_failure_clears_setup(monkeypatch):
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda rom=None: False)
    with broker._lock:
        broker._state["setup"] = True
        broker._state["launch_in_progress"] = True
    broker._do_setup()
    with broker._lock:
        assert broker._state["setup"] is False
        assert broker._state["launch_in_progress"] is False
        assert broker._state["launch_error"]


def test_do_setup_qmp_timeout_stops_and_clears(monkeypatch):
    calls = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda rom=None: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: False)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["setup"] = True
        broker._state["launch_in_progress"] = True
    broker._do_setup()
    assert calls == ["kill"]
    with broker._lock:
        assert broker._state["setup"] is False
        assert broker._state["launch_error"]


def test_do_setup_qmp_timeout_spares_a_reused_instance(monkeypatch):
    """Setup reused a live xemu, so a QMP hiccup is not licence to kill it —
    that instance may be running someone's game."""
    calls = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(
        broker, "_launch_xemu", lambda rom=None: pytest.fail("a live instance must be reused")
    )
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: False)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["setup"] = True
        broker._state["launch_in_progress"] = True
    broker._do_setup()
    assert calls == []
    with broker._lock:
        assert broker._state["setup"] is False
        assert broker._state["launch_error"]


# ── ROM launch path ───────────────────────────────────────────────────────────


def test_launch_xemu_puts_the_disc_in_the_drive_at_power_on(monkeypatch, tmp_path):
    argv = []

    class _Proc:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(broker, "_kill_xemu", lambda: None)
    monkeypatch.setattr(broker, "_wait_for_no_xemu", lambda: True)
    monkeypatch.setattr(broker, "QMP_SOCKET", tmp_path / "qmp.sock")
    monkeypatch.setattr(
        broker.subprocess, "Popen", lambda cmd, **kw: argv.append(cmd) or _Proc()
    )

    assert broker._launch_xemu("/romm/library/x.iso")
    assert argv[0][argv[0].index("-dvd_path") + 1] == "/romm/library/x.iso"

    argv.clear()
    assert broker._launch_xemu()
    # /setup boots to the dashboard, so it must not be handed a disc.
    assert "-dvd_path" not in argv[0]


def test_do_load_rom_qmp_timeout_kills_the_xemu_it_spawned(monkeypatch):
    """A discless xemu left behind busy-loops CPU cores with nothing to reap it."""
    calls = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda rom=None: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: False)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["launch_in_progress"] = True
    broker._do_load_rom("/romm/library/x.iso")
    assert calls == ["kill"]
    with broker._lock:
        assert broker._state["launch_error"]
        assert broker._state["rom_path"] is None
        assert broker._state["launch_in_progress"] is False


def test_do_load_rom_qmp_timeout_spares_a_reused_instance(monkeypatch):
    """QMP went away after the reuse check — that xemu is not ours to kill."""
    calls = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(
        broker, "_launch_xemu", lambda rom=None: pytest.fail("a live instance must be reused")
    )
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: False)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["launch_in_progress"] = True
        broker._state["rom_path"] = "/romm/library/someone-elses.iso"
    broker._do_load_rom("/romm/library/x.iso")
    assert calls == []  # the running game survives
    with broker._lock:
        assert broker._state["launch_error"]
        assert broker._state["launch_in_progress"] is False


def test_do_load_rom_boots_a_cold_start_from_the_disc_without_resetting(monkeypatch):
    """A cold start must take the disc at power-on. Injecting it afterwards
    costs a system_reset, and QMP answers while the guest is still inside the
    MCPX bootrom — a reset landing there wedges the machine, which then burns a
    core forever showing a black screen and playing silence."""
    seen = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda rom=None: seen.append(rom) or True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(
        broker, "_qmp_load_rom", lambda p: pytest.fail("a cold start must not be reset")
    )
    with broker._lock:
        broker._state["launch_in_progress"] = True
    broker._do_load_rom("/romm/library/x.iso")
    assert seen == ["/romm/library/x.iso"]
    with broker._lock:
        assert broker._state["rom_path"] == "/romm/library/x.iso"
        assert broker._state["launch_error"] is None
        assert broker._state["launch_in_progress"] is False


def test_do_load_rom_resumes_a_slot_after_a_cold_start(monkeypatch):
    """The disc arriving at power-on rather than by injection must not cost the
    resume: the snapshot still restores a machine already running this disc."""
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda rom=None: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_qmp_load_state", lambda s: True)
    with broker._lock:
        broker._state["launch_in_progress"] = True
    broker._do_load_rom("/romm/library/x.iso", load_slot=3)
    with broker._lock:
        assert broker._state["rom_path"] == "/romm/library/x.iso"
        assert broker._state["resume_error"] is None


def test_do_load_rom_failed_load_spares_a_reused_instance(monkeypatch):
    """Our disc not going in is no reason to end the game already running."""
    calls = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: False)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["launch_in_progress"] = True
        broker._state["rom_path"] = "/romm/library/someone-elses.iso"
    broker._do_load_rom("/romm/library/x.iso")
    assert calls == []
    with broker._lock:
        assert broker._state["launch_error"]


def test_do_load_rom_records_a_failed_resume_without_failing_the_launch(monkeypatch):
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)
    monkeypatch.setattr(broker, "_qmp_load_state", lambda s: False)
    with broker._lock:
        broker._state["launch_in_progress"] = True
    broker._do_load_rom("/romm/library/x.iso", load_slot=3)
    with broker._lock:
        assert broker._state["rom_path"] == "/romm/library/x.iso"
        assert broker._state["launch_error"] is None  # the ROM itself loaded
        assert "slot 3" in broker._state["resume_error"]


def test_do_load_rom_leaves_resume_error_clear_on_success(monkeypatch):
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)
    monkeypatch.setattr(broker, "_qmp_load_state", lambda s: True)
    with broker._lock:
        broker._state["launch_in_progress"] = True
        broker._state["resume_error"] = "stale error from a previous launch"
    broker._do_load_rom("/romm/library/x.iso", load_slot=3)
    with broker._lock:
        assert broker._state["resume_error"] is None


def test_do_load_rom_holds_the_save_flag_across_the_resume(monkeypatch):
    """A resume is a snapshot job on the same vmstate a save writes; running
    both at once corrupts it, so the resume claims the save flag."""
    seen = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)

    def _observe(slot):
        with broker._lock:
            seen.append(broker._state["save_in_progress"])
        return True

    monkeypatch.setattr(broker, "_qmp_load_state", _observe)
    with broker._lock:
        broker._state["launch_in_progress"] = True
    broker._do_load_rom("/romm/library/x.iso", load_slot=3)
    assert seen == [True]
    with broker._lock:
        assert broker._state["save_in_progress"] is False  # and it is given back


def test_do_load_rom_skips_the_resume_once_superseded(monkeypatch):
    """A save-and-exit or stop landing while the disc went in wins: replaying a
    snapshot into a session the user already ended is the corruption case."""
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: None)

    def _load_rom(path):
        with broker._lock:
            broker._state["session_generation"] += 1
        return True

    monkeypatch.setattr(broker, "_qmp_load_rom", _load_rom)
    monkeypatch.setattr(
        broker, "_qmp_load_state", lambda s: pytest.fail("resumed a dead session")
    )
    with broker._lock:
        broker._state["launch_in_progress"] = True
    broker._do_load_rom("/romm/library/x.iso", load_slot=3, generation=0)
    with broker._lock:
        assert broker._state["save_in_progress"] is False
        assert broker._state["rom_path"] is None


# ── HTTP contract ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    """A live broker server with all xemu/QMP interaction mocked out."""
    monkeypatch.setattr(broker, "SECRET", "")
    monkeypatch.setattr(broker, "SETUP_TIMEOUT", 30.0)
    monkeypatch.setattr(broker, "_qmp_available", lambda: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_launch_xemu", lambda rom=None: True)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), broker.BrokerHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _req(base, method, path, body=None, secret=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if secret is not None:
        req.add_header("X-Broker-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _status(base):
    return _req(base, "GET", "/status")[1]


def _wait_setup(base, want, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _status(base)["setup"] is want:
            return True
        time.sleep(0.05)
    return False


def _wait_active(base, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _status(base)["active"]:
            return True
        time.sleep(0.05)
    return False


def test_status_has_setup_key_default_false(client):
    code, body = _req(client, "GET", "/status")
    assert code == 200
    assert body["setup"] is False
    assert body["active"] is False


def test_setup_starts_and_shows_in_status(client):
    code, body = _req(client, "POST", "/setup")
    assert code == 200
    assert body["status"] == "starting setup"
    assert _wait_setup(client, True)
    st = _status(client)
    assert st["setup"] is True
    assert st["active"] is False  # no ROM loaded during setup


def test_setup_conflicts_with_active_game(client):
    with broker._lock:
        broker._state["rom_path"] = "/romm/x.iso"
    code, _ = _req(client, "POST", "/setup")
    assert code == 409


def test_setup_idempotent_when_already_active(client):
    with broker._lock:
        broker._state["setup"] = True
    code, body = _req(client, "POST", "/setup")
    assert code == 200
    assert body.get("already") is True


def test_delete_launch_ends_setup(client):
    _req(client, "POST", "/setup")
    assert _wait_setup(client, True)
    code, _ = _req(client, "DELETE", "/launch")
    assert code == 200
    assert _status(client)["setup"] is False


# ── Snapshot job wait budget ──────────────────────────────────────────────────


def test_snapshot_wait_stays_within_one_qmp_wait(tmp_path, monkeypatch):
    """A late event used to buy the next recv a second full QMP_WAIT."""
    sock_path = tmp_path / "qmp.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    received = []

    def _serve():
        conn, _ = srv.accept()
        conn.settimeout(15)
        conn.sendall(b'{"QMP": {"version": {}}}\n')
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    cmd = json.loads(line).get("execute")
                    received.append(cmd)
                    conn.sendall(b'{"return": []}\n')
                    if cmd == "snapshot-save":
                        # One unrelated event late in the window, then silence.
                        time.sleep(0.8)
                        conn.sendall(json.dumps({
                            "event": "JOB_STATUS_CHANGE",
                            "data": {"id": "other", "status": "running"},
                        }).encode() + b"\n")
        except OSError:
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    monkeypatch.setattr(broker, "QMP_SOCKET", sock_path)
    monkeypatch.setattr(broker, "QMP_TIMEOUT", 2.0)
    monkeypatch.setattr(broker, "QMP_WAIT", 1.0)
    monkeypatch.setattr(broker, "_qmp_get_hdd_node", lambda: "node0")

    start = time.monotonic()
    assert broker._qmp_snapshot("snapshot-save", "broker-slot-1") is False
    elapsed = time.monotonic() - start
    srv.close()
    assert elapsed < 1.5, f"wait ran {elapsed:.2f}s, over the {broker.QMP_WAIT}s budget"
    assert "job-cancel" in received  # the timeout still tears the job down


def test_reset_wait_stays_within_one_qmp_wait(tmp_path, monkeypatch):
    """Same overrun as the snapshot job: a message arriving late in the window
    used to buy the next recv another full QMP_WAIT, and reset retries."""
    sock_path = tmp_path / "qmp.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def _serve():
        conn, _ = srv.accept()
        conn.settimeout(15)
        conn.sendall(b'{"QMP": {"version": {}}}\n')
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    cmd = json.loads(line).get("execute")
                    conn.sendall(b'{"return": {}}\n')
                    if cmd == "system_reset":
                        # An unrelated event late in the window, then silence:
                        # the RESET the caller waits for never arrives.
                        time.sleep(0.8)
                        conn.sendall(b'{"event": "STOP"}\n')
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    monkeypatch.setattr(broker, "QMP_SOCKET", sock_path)
    monkeypatch.setattr(broker, "QMP_TIMEOUT", 2.0)
    monkeypatch.setattr(broker, "QMP_WAIT", 1.0)

    start = time.monotonic()
    assert broker._qmp_reset_confirmed(retries=1) is False
    elapsed = time.monotonic() - start
    srv.close()
    assert elapsed < 1.5, f"wait ran {elapsed:.2f}s, over the {broker.QMP_WAIT}s budget"


def _serve_endless_events(sock_path, quiet_after):
    """A QMP peer that answers `quiet_after` commands and then only ever emits
    async events — the shape that used to re-arm QMP_WAIT on every recv."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def _serve():
        conn, _ = srv.accept()
        conn.settimeout(15)
        conn.sendall(b'{"QMP": {"version": {}}}\n')
        answered = 0
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    del line
                    if answered < quiet_after:
                        answered += 1
                        conn.sendall(b'{"return": {}}\n')
                        continue
                    # Chatter forever, always inside the per-recv timeout.
                    for _ in range(30):
                        time.sleep(0.2)
                        conn.sendall(b'{"event": "RTC_CHANGE"}\n')
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    return srv


def test_snapshot_command_drain_gives_up_on_a_peer_that_never_returns(tmp_path, monkeypatch):
    """send_cmd used to re-arm QMP_WAIT per recv, so a chatty peer pinned the
    handler thread for good instead of failing the snapshot."""
    sock_path = tmp_path / "qmp.sock"
    srv = _serve_endless_events(sock_path, quiet_after=2)  # capabilities, job-dismiss
    monkeypatch.setattr(broker, "QMP_SOCKET", sock_path)
    monkeypatch.setattr(broker, "QMP_TIMEOUT", 2.0)
    monkeypatch.setattr(broker, "QMP_WAIT", 1.0)
    monkeypatch.setattr(broker, "_qmp_get_hdd_node", lambda: "node0")

    start = time.monotonic()
    try:
        assert broker._qmp_snapshot("snapshot-save", "broker-slot-1") is False
    finally:
        srv.close()
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"send_cmd ran {elapsed:.2f}s on a {broker.QMP_WAIT}s budget"


def test_reset_capability_drain_gives_up_on_a_peer_that_never_returns(tmp_path, monkeypatch):
    """Same overrun in the reset path's drain-to-return loop."""
    sock_path = tmp_path / "qmp.sock"
    srv = _serve_endless_events(sock_path, quiet_after=0)  # not even capabilities
    monkeypatch.setattr(broker, "QMP_SOCKET", sock_path)
    monkeypatch.setattr(broker, "QMP_TIMEOUT", 2.0)
    monkeypatch.setattr(broker, "QMP_WAIT", 1.0)

    start = time.monotonic()
    try:
        assert broker._qmp_reset_confirmed(retries=1) is False
    finally:
        srv.close()
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"drain ran {elapsed:.2f}s on a {broker.QMP_WAIT}s budget"


def _serve_snapshot_job(sock_path, job_error=None, job_id="broker-slot-1"):
    """A QMP peer that concludes a snapshot job, optionally with an error.

    Returns (server_socket, received) — `received` accumulates executed commands
    so the teardown sequence can be asserted on."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    received = []

    def _event(jid, status):
        return json.dumps({
            "event": "JOB_STATUS_CHANGE", "data": {"id": jid, "status": status},
        }).encode() + b"\n"

    def _serve():
        conn, _ = srv.accept()
        conn.settimeout(15)
        conn.sendall(b'{"QMP": {"version": {}}}\n')
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    cmd = json.loads(line).get("execute")
                    received.append(cmd)
                    if cmd == "query-jobs":
                        job = {"id": job_id, "status": "concluded"}
                        if job_error is not None:
                            job["error"] = job_error
                        conn.sendall(json.dumps({"return": [job]}).encode() + b"\n")
                    else:
                        conn.sendall(b'{"return": {}}\n')
                    if cmd == "snapshot-save":
                        # Another job concludes first: the wait keys on the id.
                        conn.sendall(_event("someone-elses-job", "concluded"))
                        conn.sendall(_event(job_id, "concluded"))
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    return srv, received


@pytest.fixture
def snapshot_qmp(tmp_path, monkeypatch):
    """Point the snapshot helper at a scratch QMP socket with a short budget."""
    monkeypatch.setattr(broker, "QMP_SOCKET", tmp_path / "qmp.sock")
    monkeypatch.setattr(broker, "QMP_TIMEOUT", 2.0)
    monkeypatch.setattr(broker, "QMP_WAIT", 2.0)
    monkeypatch.setattr(broker, "_qmp_get_hdd_node", lambda: "node0")
    return tmp_path / "qmp.sock"


def test_qmp_snapshot_succeeds_when_the_job_concludes(snapshot_qmp):
    """RomM pulls a state file on the strength of this return value."""
    srv, received = _serve_snapshot_job(snapshot_qmp)
    try:
        assert broker._qmp_snapshot("snapshot-save", "broker-slot-1") is True
    finally:
        srv.close()
    assert "snapshot-save" in received
    assert "query-jobs" in received     # the job is asked whether it errored
    assert received[-1] == "job-dismiss"  # and a concluded job is cleared away
    assert "job-cancel" not in received


def test_qmp_snapshot_fails_when_the_job_concludes_with_an_error(snapshot_qmp):
    """A concluded job is not a successful one — QEMU reports the failure in
    query-jobs, and reporting True here would have RomM pull an empty state."""
    srv, received = _serve_snapshot_job(snapshot_qmp, job_error="No space left on device")
    try:
        assert broker._qmp_snapshot("snapshot-save", "broker-slot-1") is False
    finally:
        srv.close()
    assert received[-1] == "job-dismiss"  # still cleared, so the id is reusable


def test_qmp_snapshot_passes_the_node_and_tag_through(snapshot_qmp):
    """snapshot-save must name the block node and carry the vmstate with it."""
    sent = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(snapshot_qmp))
    srv.listen(1)

    def _serve():
        conn, _ = srv.accept()
        conn.settimeout(15)
        conn.sendall(b'{"QMP": {"version": {}}}\n')
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    msg = json.loads(line)
                    sent.append(msg)
                    conn.sendall(b'{"return": []}\n')
                    if msg.get("execute") == "snapshot-save":
                        conn.sendall(json.dumps({
                            "event": "JOB_STATUS_CHANGE",
                            "data": {"id": "broker-slot-7", "status": "concluded"},
                        }).encode() + b"\n")
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    try:
        assert broker._qmp_snapshot("snapshot-save", "broker-slot-7") is True
    finally:
        srv.close()
    save = next(m for m in sent if m.get("execute") == "snapshot-save")
    assert save["arguments"] == {
        "job-id": "broker-slot-7",
        "tag": "broker-slot-7",
        "devices": ["node0"],
        "vmstate": "node0",
    }


# ── ROM path validation ───────────────────────────────────────────────────────


@pytest.fixture
def rom_root(tmp_path, monkeypatch):
    root = tmp_path / "library"
    (root / "xbox").mkdir(parents=True)
    monkeypatch.setattr(broker, "ROM_ROOT", root.resolve())
    return root


def test_validate_rom_path_accepts_path_inside_root(rom_root):
    rom = rom_root / "xbox" / "game.iso"
    rom.write_bytes(b"iso")
    assert broker._validate_rom_path(str(rom)) == rom.resolve()


def test_validate_rom_path_normalises_traversal_back_inside(rom_root):
    raw = str(rom_root / "xbox" / ".." / "game.iso")
    assert broker._validate_rom_path(raw) == (rom_root / "game.iso").resolve()


def test_validate_rom_path_rejects_traversal_outside_root(rom_root):
    raw = str(rom_root / "xbox" / ".." / ".." / ".." / "etc" / "passwd")
    assert broker._validate_rom_path(raw) is None


def test_validate_rom_path_rejects_absolute_path_outside_root(rom_root):
    assert broker._validate_rom_path("/etc/passwd") is None


def test_validate_rom_path_rejects_symlink_escaping_root(rom_root, tmp_path):
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"iso")
    link = rom_root / "escape.iso"
    link.symlink_to(outside)
    assert broker._validate_rom_path(str(link)) is None


# ── State file archives ───────────────────────────────────────────────────────


@pytest.fixture
def hdd(tmp_path, monkeypatch):
    """Point HDD_IMAGE at a scratch file the test user can chown."""
    path = tmp_path / "xemu" / "xbox_hdd.qcow2"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original image")
    monkeypatch.setattr(broker, "HDD_IMAGE", path)
    monkeypatch.setattr(broker, "_ABC_UID", os.getuid())
    monkeypatch.setattr(broker, "_ABC_GID", os.getgid())
    return path


def _zip_members(content):
    """{name: bytes} for every member of a zip the broker produced."""
    assert content is not None
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _zip_bytes(members, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def _state_archive(data=b"restored image"):
    return _zip_bytes([(broker.HDD_IMAGE_ENTRY, data)])


def _bad_crc_archive(data=b"restored image"):
    """A structurally valid archive whose payload no longer matches its CRC.

    Stored (not deflated) so a flipped payload byte survives the header checks
    and only blows up inside the copy, which is the case gripe 1 is about."""
    raw = bytearray(_zip_bytes([(broker.HDD_IMAGE_ENTRY, data)], zipfile.ZIP_STORED))
    name_len = int.from_bytes(raw[26:28], "little")
    extra_len = int.from_bytes(raw[28:30], "little")
    raw[30 + name_len + extra_len] ^= 0xFF
    return bytes(raw)


def _tmp_image(hdd):
    return hdd.parent / f".{hdd.name}.tmp"


def test_zip_hdd_image_round_trip(hdd):
    content = broker._zip_hdd_image()
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        assert zf.namelist() == [broker.HDD_IMAGE_ENTRY]
        assert zf.read(broker.HDD_IMAGE_ENTRY) == b"original image"


def test_zip_hdd_image_returns_none_when_missing(hdd):
    hdd.unlink()
    assert broker._zip_hdd_image() is None


def test_restore_hdd_image_round_trip(hdd):
    payload = b"a new disk image" * 64
    assert broker._restore_hdd_image(_state_archive(payload)) is None
    assert hdd.read_bytes() == payload
    assert not _tmp_image(hdd).exists()


def test_restore_hdd_image_creates_missing_parent(tmp_path, monkeypatch):
    path = tmp_path / "fresh" / "xbox_hdd.qcow2"
    monkeypatch.setattr(broker, "HDD_IMAGE", path)
    monkeypatch.setattr(broker, "_ABC_UID", os.getuid())
    monkeypatch.setattr(broker, "_ABC_GID", os.getgid())
    assert broker._restore_hdd_image(_state_archive(b"seed")) is None
    assert path.read_bytes() == b"seed"


def test_restore_hdd_image_rejects_non_zip(hdd):
    error = broker._restore_hdd_image(b"this is not a zip archive at all")
    assert error == "body is not a zip archive"
    assert hdd.read_bytes() == b"original image"


def test_restore_hdd_image_rejects_wrong_member_name(hdd):
    error = broker._restore_hdd_image(_zip_bytes([("something_else.qcow2", b"x")]))
    assert error and broker.HDD_IMAGE_ENTRY in error
    assert hdd.read_bytes() == b"original image"


def test_restore_hdd_image_rejects_extra_members(hdd):
    error = broker._restore_hdd_image(
        _zip_bytes([(broker.HDD_IMAGE_ENTRY, b"x"), ("notes.txt", b"y")])
    )
    assert error and broker.HDD_IMAGE_ENTRY in error
    assert hdd.read_bytes() == b"original image"


def test_restore_hdd_image_rejects_oversized_member(hdd, monkeypatch):
    # Declared (uncompressed) size is what matters: the archive itself is tiny.
    monkeypatch.setattr(broker, "HDD_IMAGE_MAX_BYTES", 128)
    error = broker._restore_hdd_image(_state_archive(b"\0" * 4096))
    assert error == "archive exceeds size limit when extracted"
    assert hdd.read_bytes() == b"original image"
    assert not _tmp_image(hdd).exists()


def test_restore_hdd_image_rejects_corrupt_member(hdd):
    error = broker._restore_hdd_image(_bad_crc_archive())
    assert error and "corrupt" in error
    assert hdd.read_bytes() == b"original image"
    assert not _tmp_image(hdd).exists()  # the aborted copy is not left behind


# ── /state-file HTTP contract ─────────────────────────────────────────────────


def _raw_req(base, method, path, data=None, secret=None):
    req = urllib.request.Request(base + path, data=data, method=method)
    if secret is not None:
        req.add_header("X-Broker-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
def state_client(client, hdd, monkeypatch):
    """The broker server with slot 3 holding a snapshot and pause/resume working."""
    monkeypatch.setattr(
        broker, "_qmp_snapshot_tags", lambda: (str(hdd), {"broker-slot-3"})
    )
    monkeypatch.setattr(broker, "_qmp_pause", lambda: True)
    monkeypatch.setattr(broker, "_qmp_resume", lambda: True)
    monkeypatch.setattr(broker, "STATE_GET_WAIT", 0.3)
    return client


@pytest.fixture
def restore_client(client, hdd, monkeypatch):
    """The broker server with xemu down, which is what a restore requires."""
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    return client


def _wait_state_file_idle(timeout=5.0):
    """Block until the request handler has run its finally block.

    The response is written from inside the handler, so a client that has read
    the last byte has not thereby waited for the flag to be given back."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with broker._lock:
            if not broker._state["state_file_in_progress"]:
                return True
        time.sleep(0.05)
    return False


def test_get_state_file_serves_zipped_image(state_client, hdd):
    code, headers, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 200
    assert headers["Content-Type"] == "application/zip"
    assert headers["X-State-Filename"] == "xemu.x03"
    assert "X-Xemu-Paused" not in headers
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert zf.read(broker.HDD_IMAGE_ENTRY) == b"original image"
    assert _wait_state_file_idle()


def test_get_state_file_uses_rom_name(state_client):
    with broker._lock:
        broker._state["rom_name"] = "Halo"
    _, headers, _ = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert headers["X-State-Filename"] == "Halo.x03"


def test_get_state_file_rejects_bad_slot(state_client):
    assert _raw_req(state_client, "GET", "/state-file?slot=abc")[0] == 400
    assert _raw_req(state_client, "GET", "/state-file?slot=99")[0] == 400


def test_get_state_file_404_when_slot_empty(state_client):
    code, _, _ = _raw_req(state_client, "GET", "/state-file?slot=4")
    assert code == 404
    assert _wait_state_file_idle()


def test_get_state_file_503_when_snapshot_query_fails(state_client, monkeypatch):
    # A failed query-block must not be reported as "this slot holds no state".
    monkeypatch.setattr(broker, "_qmp_snapshot_tags", lambda: None)
    code, _, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 503
    assert "could not query" in json.loads(body)["error"]


def test_get_state_file_500_when_xemu_has_another_image_open(state_client, monkeypatch):
    """hdd_path in xemu.toml is user-set: if it does not point at HDD_IMAGE the
    snapshot lives in one file and the broker would zip a different one."""
    monkeypatch.setattr(
        broker, "_qmp_snapshot_tags",
        lambda: ("/config/xemu/somewhere_else.qcow2", {"broker-slot-3"}),
    )
    code, _, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 500  # not a 200 with an archive that lacks the capture
    error = json.loads(body)
    assert "different hard disk image" in error["error"]
    assert error["xemu_image"] == "/config/xemu/somewhere_else.qcow2"
    assert error["broker_image"] == str(broker.HDD_IMAGE)
    assert _wait_state_file_idle()


def test_get_state_file_500_when_qemu_reports_no_filename(state_client, monkeypatch):
    """An unnamed image cannot be shown to be the one being served."""
    monkeypatch.setattr(broker, "_qmp_snapshot_tags", lambda: ("", {"broker-slot-3"}))
    assert _raw_req(state_client, "GET", "/state-file?slot=3")[0] == 500


def test_same_image_matches_through_a_symlink(tmp_path):
    """hdd_path is commonly a link into /config; that is still the same file."""
    real = tmp_path / "xbox_hdd.qcow2"
    real.write_bytes(b"image")
    link = tmp_path / "linked.qcow2"
    link.symlink_to(real)
    assert broker._same_image(str(link), real) is True
    assert broker._same_image(str(tmp_path / "other.qcow2"), real) is False


def test_get_state_file_503_when_pause_fails(state_client, monkeypatch):
    monkeypatch.setattr(broker, "_qmp_pause", lambda: False)
    assert _raw_req(state_client, "GET", "/state-file?slot=3")[0] == 503


def test_get_state_file_flags_a_failed_resume(state_client, monkeypatch):
    monkeypatch.setattr(broker, "_qmp_resume", lambda: False)
    code, headers, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 200  # the image was read fine
    assert headers["X-Xemu-Paused"] == "true"  # but xemu is still paused
    assert len(body) > 0


def test_get_state_file_409_while_launch_in_progress(state_client):
    with broker._lock:
        broker._state["launch_in_progress"] = True
    code, _, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 409
    assert json.loads(body)["error"] == "launch in progress"


def test_get_state_file_409_while_another_transfer_runs(state_client):
    with broker._lock:
        broker._state["state_file_in_progress"] = True
    code, _, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 409
    assert "already in progress" in json.loads(body)["error"]
    with broker._lock:
        assert broker._state["state_file_in_progress"] is True  # not stolen


def test_get_state_file_409_when_save_never_finishes(state_client):
    with broker._lock:
        broker._state["save_in_progress"] = True
    code, _, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 409
    assert json.loads(body)["error"] == "save still in progress"


def test_get_state_file_blocks_a_concurrent_save(state_client, monkeypatch):
    """A save must not start while xemu is paused mid-transfer."""
    entered = threading.Event()
    release = threading.Event()

    def _slow_zip(keep_tag=None):
        entered.set()
        release.wait(timeout=10)
        return b"PK\x03\x04 pretend zip"

    monkeypatch.setattr(broker, "_zip_hdd_image", _slow_zip)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"

    result = {}
    getter = threading.Thread(
        target=lambda: result.update(
            code=_raw_req(state_client, "GET", "/state-file?slot=3")[0]
        )
    )
    getter.start()
    try:
        assert entered.wait(timeout=5)
        code, body = _req(state_client, "POST", "/save-state", {"slot": 3})
        assert code == 409
        assert body["error"] == "state-file transfer in progress"
        assert _req(state_client, "POST", "/load-state", {"slot": 3})[0] == 409
        assert _req(state_client, "POST", "/save-and-exit", {"slot": 3})[0] == 409
        assert _req(state_client, "POST", "/setup")[0] == 409
    finally:
        release.set()
        getter.join(timeout=10)
    assert result["code"] == 200
    assert _wait_state_file_idle()


def test_put_state_file_restores_image(restore_client, hdd):
    payload = b"pushed disk image" * 32
    code, _, body = _raw_req(
        restore_client, "PUT", "/state-file?filename=Halo.x03", _state_archive(payload)
    )
    assert code == 200
    assert json.loads(body)["filename"] == "Halo.x03"
    assert hdd.read_bytes() == payload
    assert _wait_state_file_idle()


def test_put_state_file_rejects_bad_filename(restore_client, hdd):
    code, _, _ = _raw_req(
        restore_client, "PUT", "/state-file?filename=Halo.zip", _state_archive()
    )
    assert code == 400
    assert hdd.read_bytes() == b"original image"


def test_put_state_file_rejects_non_zip_body(restore_client, hdd):
    code, _, body = _raw_req(
        restore_client, "PUT", "/state-file?filename=Halo.x03", b"nonsense payload"
    )
    assert code == 400
    assert json.loads(body)["error"] == "body is not a zip archive"
    assert hdd.read_bytes() == b"original image"


def test_put_state_file_rejects_corrupt_archive(restore_client, hdd):
    code, _, body = _raw_req(
        restore_client, "PUT", "/state-file?filename=Halo.x03", _bad_crc_archive()
    )
    assert code == 400  # not an unhandled 500
    assert "corrupt" in json.loads(body)["error"]
    assert hdd.read_bytes() == b"original image"
    assert not _tmp_image(hdd).exists()
    assert _wait_state_file_idle()


def test_put_state_file_409_while_xemu_runs(client, hdd):
    code, _, _ = _raw_req(
        client, "PUT", "/state-file?filename=Halo.x03", _state_archive()
    )
    assert code == 409


def test_put_state_file_409_when_xemu_comes_up_during_the_upload(
    restore_client, hdd, monkeypatch
):
    """A 256 MB body takes minutes to read, and the early check is that old by
    the time the flag is claimed — a launch fits entirely inside the window."""
    alive = [False]
    monkeypatch.setattr(broker, "_qmp_available", lambda: alive[0])
    real_read = broker.BrokerHandler._read_state_body

    def _read_then_launch(self):
        content = real_read(self)
        alive[0] = True  # a launch started and finished during the read
        return content

    monkeypatch.setattr(broker.BrokerHandler, "_read_state_body", _read_then_launch)
    code, _, body = _raw_req(
        restore_client, "PUT", "/state-file?filename=Halo.x03", _state_archive()
    )
    assert code == 409
    assert "xemu is running" in json.loads(body)["error"]
    assert hdd.read_bytes() == b"original image"  # the live qcow2 is untouched
    assert _wait_state_file_idle()


def test_put_state_file_409_while_saving(restore_client, hdd):
    with broker._lock:
        broker._state["save_in_progress"] = True
    code, _, body = _raw_req(
        restore_client, "PUT", "/state-file?filename=Halo.x03", _state_archive()
    )
    assert code == 409
    assert json.loads(body)["error"] == "save still in progress"
    assert hdd.read_bytes() == b"original image"


def test_concurrent_puts_are_serialised(restore_client, hdd, monkeypatch):
    """Two restores share one temp file, so the second must be turned away."""
    entered = threading.Event()
    release = threading.Event()
    concurrent = []
    inside = []

    def _slow_restore(content):
        inside.append(1)
        concurrent.append(len(inside))
        entered.set()
        release.wait(timeout=10)
        inside.pop()
        return None

    monkeypatch.setattr(broker, "_restore_hdd_image", _slow_restore)
    body = _state_archive()
    first = {}
    runner = threading.Thread(
        target=lambda: first.update(
            code=_raw_req(restore_client, "PUT", "/state-file?filename=a.x01", body)[0]
        )
    )
    runner.start()
    try:
        assert entered.wait(timeout=5)
        code, _, second = _raw_req(
            restore_client, "PUT", "/state-file?filename=b.x02", body
        )
        assert code == 409
        assert "already in progress" in json.loads(second)["error"]
        assert _req(restore_client, "POST", "/setup")[0] == 409
    finally:
        release.set()
        runner.join(timeout=10)
    assert first["code"] == 200
    assert concurrent == [1]  # never two restores at once


def test_launch_409_during_state_file_transfer(client, rom_root):
    rom = rom_root / "xbox" / "game.iso"
    rom.write_bytes(b"iso")
    with broker._lock:
        broker._state["state_file_in_progress"] = True
    code, body = _req(client, "POST", "/launch", {"rom_path": str(rom)})
    assert code == 409
    assert body["error"] == "state-file transfer in progress"


def test_status_surfaces_a_failed_resume_on_an_active_session(client, rom_root, monkeypatch):
    """The client polls /status; a game that booted fresh must not look normal."""
    rom = rom_root / "xbox" / "game.iso"
    rom.write_bytes(b"iso")
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)
    monkeypatch.setattr(broker, "_qmp_load_state", lambda s: False)
    code, _ = _req(client, "POST", "/launch", {"rom_path": str(rom), "load_slot": 3})
    assert code == 200
    assert _wait_active(client)
    st = _status(client)
    assert st["active"] is True          # the session really is running
    assert st["launch_error"] is None    # the launch itself did not fail
    assert "slot 3" in st["resume_error"]


@pytest.mark.parametrize("flag", ["save_in_progress", "state_file_reading"])
def test_status_does_not_probe_qmp_while_a_job_holds_the_monitor(client, monkeypatch, flag):
    """xemu serves one QMP client at a time: a routine poll landing mid-job used
    to stall for QMP_TIMEOUT and then report a live session as stopped."""
    probes = []

    def _stalled_probe():
        probes.append(1)
        time.sleep(5)  # what a monitor already owned by a job really does
        return False

    monkeypatch.setattr(broker, "_qmp_available", _stalled_probe)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
        broker._state[flag] = True
    start = time.monotonic()
    st = _status(client)
    assert time.monotonic() - start < 1.0
    assert probes == []               # the monitor was left alone
    assert st["xemu_running"] is True  # a job in flight implies a live xemu
    assert st["active"] is True


def test_status_still_probes_during_a_state_file_restore(client, monkeypatch):
    """A PUT /state-file holds state_file_in_progress with xemu DOWN, so it must
    not be read as "a job is running, xemu must be up" — that would report a
    stopped emulator as live for the length of the restore."""
    probes = []

    def _probe():
        probes.append(1)
        return False

    monkeypatch.setattr(broker, "_qmp_available", _probe)
    with broker._lock:
        broker._state["state_file_in_progress"] = True   # PUT direction
        broker._state["state_file_reading"] = False
    st = _status(client)
    assert probes == [1]
    assert st["xemu_running"] is False


def test_get_state_file_413_without_zipping_an_oversized_image(state_client, hdd, monkeypatch):
    """The archive is built in memory, so the limit is enforced before zipping."""
    zipped = []
    paused = []
    monkeypatch.setattr(broker, "_zip_hdd_image", lambda keep_tag=None: zipped.append(1))
    monkeypatch.setattr(broker, "_qmp_pause", lambda: paused.append(1) or True)
    monkeypatch.setattr(broker, "HDD_IMAGE_MAX_BYTES", 4)  # hdd holds 14 bytes
    code, _, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 413
    assert json.loads(body)["error"] == "state file exceeds size limit"
    assert zipped == []  # no compressed copy was ever built
    assert paused == []  # and the guest was never stopped for it
    assert _wait_state_file_idle()


# ── qcow2 snapshot table ──────────────────────────────────────────────────────


def _qcow2(path: Path, tags, *, nb_override=None, truncate=0):
    """Write a qcow2 whose snapshot table holds `tags`.

    Only the fields _qcow2_snapshot_tags reads are filled in; the rest of the
    header is zeroes, which is enough because nothing here opens the image.
    """
    entries = b""
    for i, tag in enumerate(tags):
        name = tag.encode()
        id_str = str(i + 1).encode()
        extra = b""
        entries += struct.pack(
            ">QIHHIIQII", 0, 0, len(id_str), len(name), 0, 0, 0, 0, len(extra)
        )
        entries += extra + id_str + name
        entries += b"\x00" * (-(len(extra) + len(id_str) + len(name)) % 8)

    table_offset = 512
    header = bytearray(b"\x00" * table_offset)
    header[0:4] = b"QFI\xfb"
    header[4:8] = (3).to_bytes(4, "big")
    count = len(tags) if nb_override is None else nb_override
    header[60:64] = count.to_bytes(4, "big")
    header[64:72] = table_offset.to_bytes(8, "big")
    blob = bytes(header) + entries
    path.write_bytes(blob[:len(blob) - truncate] if truncate else blob)
    return path


def test_qcow2_snapshot_tags_reads_the_table(tmp_path):
    """Names of differing length exercise the 8-byte padding between entries."""
    img = _qcow2(tmp_path / "d.qcow2", ["broker-slot-1", "broker-slot-10", "manual"])
    assert broker._qcow2_snapshot_tags(img) == {
        "broker-slot-1", "broker-slot-10", "manual",
    }


def test_qcow2_snapshot_tags_empty_table_is_not_a_failure(tmp_path):
    """An empty set means "no states"; None means "could not tell" — the caller
    turns the second into a 503, so they must not be conflated."""
    assert broker._qcow2_snapshot_tags(_qcow2(tmp_path / "d.qcow2", [])) == set()


def test_qcow2_snapshot_tags_rejects_a_non_qcow2(tmp_path):
    path = tmp_path / "d.qcow2"
    path.write_bytes(b"not an image at all, but long enough to read 72 bytes......")
    assert broker._qcow2_snapshot_tags(path) is None


def test_qcow2_snapshot_tags_none_on_missing_file(tmp_path):
    assert broker._qcow2_snapshot_tags(tmp_path / "gone.qcow2") is None


def test_qcow2_snapshot_tags_none_on_truncated_table(tmp_path):
    """A half-written table must not read as a shorter list of states."""
    img = _qcow2(tmp_path / "d.qcow2", ["broker-slot-1", "broker-slot-2"], truncate=12)
    assert broker._qcow2_snapshot_tags(img) is None


def test_qcow2_snapshot_tags_none_when_count_overruns_the_file(tmp_path):
    img = _qcow2(tmp_path / "d.qcow2", ["broker-slot-1"], nb_override=5)
    assert broker._qcow2_snapshot_tags(img) is None


# ── Trimming a state archive to one snapshot ──────────────────────────────────
#
# The builder and reader below are deliberately written from the qcow2 spec
# rather than from broker's own helpers: a rebuild checked with the same code
# that wrote it would agree with itself no matter how wrong it was.

_CL = 512               # smallest legal cluster, so images stay tiny
_PER_L2 = _CL // 8      # 64 entries
_GUEST_CLUSTERS = 256
_L1_SIZE = -(-_GUEST_CLUSTERS // _PER_L2)
_COPIED = 1 << 63
_COMPRESSED = 1 << 62


def _build_qcow2(path, active, snapshots=(), *, leaked=0, compressed=False):
    """Write a structurally valid qcow2.

    `active` and each snapshot mapping are {guest cluster: payload}. A snapshot
    mapping of None shares the active tables outright, which is the shape QEMU
    leaves behind right after savevm. `leaked` adds clusters that carry a
    refcount but that no table points at, which is what an interrupted snapshot
    job strands in a real image.
    """
    body, refs, nxt, l2_indices = {}, {}, [1], set()

    def take(data=b""):
        idx = nxt[0]
        nxt[0] += 1
        body[idx] = bytes(data).ljust(_CL, b"\x00")
        refs[idx] = 0
        return idx

    def build(mapping):
        tables = {}
        for guest, payload in sorted(mapping.items()):
            top = guest // _PER_L2
            if top not in tables:
                tables[top] = ({}, take())
            tables[top][0][guest % _PER_L2] = take(payload)
        l1 = [0] * _L1_SIZE
        for top, (entries, l2_idx) in tables.items():
            l1[top] = l2_idx
            l2_indices.add(l2_idx)
            packed = [0] * _PER_L2
            for j, data_idx in entries.items():
                packed[j] = data_idx * _CL
            body[l2_idx] = struct.pack(f">{_PER_L2}Q", *packed)
        return l1

    active_l1 = build(active)
    snap_l1s = [active_l1[:] if m is None else build(m) for _, m, _ in snapshots]

    l1a_idx = take()
    snap_l1_idx = [take() for _ in snap_l1s]
    table_idx = take()
    leaked_idx = [take(b"stranded by an interrupted job") for _ in range(leaked)]

    def count(l1):
        for entry in l1:
            if not entry:
                continue
            refs[entry] += 1
            for l2e in struct.unpack(f">{_PER_L2}Q", body[entry]):
                if l2e:
                    refs[l2e // _CL] += 1

    count(active_l1)
    for l1 in snap_l1s:
        count(l1)
    for idx in [l1a_idx, *snap_l1_idx, table_idx, *leaked_idx]:
        refs[idx] = 1

    for l2_idx in l2_indices:
        packed = []
        for l2e in struct.unpack(f">{_PER_L2}Q", body[l2_idx]):
            if not l2e:
                packed.append(0)
                continue
            flag = _COPIED if refs[l2e // _CL] == 1 else 0
            packed.append(l2e | flag)
        if compressed and packed:
            first = next(i for i, v in enumerate(packed) if v)
            packed[first] |= _COMPRESSED
            compressed = False
        body[l2_idx] = struct.pack(f">{_PER_L2}Q", *packed)

    def pack_l1(l1, mark):
        out = []
        for entry in l1:
            flag = _COPIED if (mark and entry and refs[entry] == 1) else 0
            out.append((entry * _CL | flag) if entry else 0)
        return struct.pack(f">{_L1_SIZE}Q", *out)

    body[l1a_idx] = pack_l1(active_l1, True).ljust(_CL, b"\x00")
    for idx, l1 in zip(snap_l1_idx, snap_l1s):
        body[idx] = pack_l1(l1, False).ljust(_CL, b"\x00")

    entries = b""
    for i, ((name, _, vm_size), idx) in enumerate(zip(snapshots, snap_l1_idx)):
        raw_name, ident = name.encode(), str(i + 1).encode()
        entries += struct.pack(">QIHHIIQII", idx * _CL, _L1_SIZE, len(ident),
                               len(raw_name), 0, 0, 0, vm_size, 0)
        entries += ident + raw_name
        entries += b"\x00" * (-(len(ident) + len(raw_name)) % 8)
    assert len(entries) <= _CL, "test snapshot table must fit one cluster"
    body[table_idx] = entries.ljust(_CL, b"\x00")

    rc_table_idx, rc_block_idx = nxt[0], nxt[0] + 1
    nxt[0] += 2
    total = nxt[0]
    assert total <= _CL // 2, "test image must fit one refcount block"
    refs[0] = refs[rc_table_idx] = refs[rc_block_idx] = 1

    counts = [0] * total
    for idx, n in refs.items():
        counts[idx] = n
    block = bytearray(_CL)
    struct.pack_into(f">{total}H", block, 0, *counts)
    body[rc_block_idx] = bytes(block)
    table = [0] * (_CL // 8)
    table[0] = rc_block_idx * _CL
    body[rc_table_idx] = struct.pack(f">{_CL // 8}Q", *table)

    header = bytearray(_CL)
    header[0:4] = b"QFI\xfb"
    header[4:8] = (3).to_bytes(4, "big")
    header[20:24] = (9).to_bytes(4, "big")
    header[24:32] = (_GUEST_CLUSTERS * _CL).to_bytes(8, "big")
    header[36:40] = _L1_SIZE.to_bytes(4, "big")
    header[40:48] = (l1a_idx * _CL).to_bytes(8, "big")
    header[48:56] = (rc_table_idx * _CL).to_bytes(8, "big")
    header[56:60] = (1).to_bytes(4, "big")
    header[60:64] = len(snapshots).to_bytes(4, "big")
    header[64:72] = (table_idx * _CL).to_bytes(8, "big")
    header[96:100] = (4).to_bytes(4, "big")
    header[100:104] = (104).to_bytes(4, "big")
    body[0] = bytes(header)

    path.write_bytes(b"".join(body.get(i, bytes(_CL)) for i in range(total)))
    return path


def _read_guest(path, snapshot=None):
    """{guest cluster: payload} for the active mapping, or for a named snapshot."""
    raw = path.read_bytes()
    mask = 0x00FFFFFFFFFFFE00
    l1_offset = int.from_bytes(raw[40:48], "big")
    l1_size = int.from_bytes(raw[36:40], "big")
    if snapshot is not None:
        pos = int.from_bytes(raw[64:72], "big")
        for _ in range(int.from_bytes(raw[60:64], "big")):
            (s_l1, s_size, id_len, name_len, _a, _b, _c,
             _vm, extra) = struct.unpack(">QIHHIIQII", raw[pos:pos + 40])
            start = pos + 40 + extra
            name = raw[start + id_len:start + id_len + name_len].decode()
            if name == snapshot:
                l1_offset, l1_size = s_l1, s_size
                break
            pos = start + id_len + name_len
            pos += -(extra + id_len + name_len) % 8
        else:
            raise AssertionError(f"{snapshot!r} not in the image")
    out = {}
    l1 = struct.unpack(f">{l1_size}Q", raw[l1_offset:l1_offset + l1_size * 8])
    for i, entry in enumerate(l1):
        l2_offset = entry & mask
        if not l2_offset:
            continue
        l2 = struct.unpack(f">{_PER_L2}Q", raw[l2_offset:l2_offset + _CL])
        for j, l2e in enumerate(l2):
            host = l2e & mask
            if host:
                out[i * _PER_L2 + j] = raw[host:host + _CL]
    return out


@pytest.fixture
def qcow(tmp_path, monkeypatch):
    """Point HDD_IMAGE at a real qcow2 the trim can be pointed at."""
    path = tmp_path / "xemu" / "xbox_hdd.qcow2"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(broker, "HDD_IMAGE", path)
    return path


def _payload(tag):
    return tag.encode().ljust(_CL, b"\x00")


def test_trim_keeps_only_the_wanted_snapshot(qcow):
    _build_qcow2(
        qcow,
        {0: _payload("disk-0"), 70: _payload("disk-70")},
        [("broker-slot-1", {0: _payload("slot1")}, 4096),
         ("broker-slot-10", {0: _payload("slot10")}, 8192),
         ("broker-slot-3", {0: _payload("slot3")}, 2048)],
    )
    trimmed = broker._trim_hdd_image("broker-slot-10")
    assert trimmed is not None
    assert broker._qcow2_snapshot_tags(trimmed) == {"broker-slot-10"}


def test_trim_preserves_every_guest_cluster(qcow):
    active = {0: _payload("disk-0"), 63: _payload("disk-63"), 200: _payload("disk-200")}
    snap = {0: _payload("vm-0"), 130: _payload("vm-130")}
    _build_qcow2(qcow, active,
                 [("broker-slot-1", {0: _payload("other")}, 512),
                  ("broker-slot-10", snap, 8192)])
    trimmed = broker._trim_hdd_image("broker-slot-10")
    assert _read_guest(trimmed) == active
    assert _read_guest(trimmed, "broker-slot-10") == snap


def test_trim_keeps_the_snapshots_vm_state_size(qcow):
    """RomM shows the size, and QEMU uses it to find the state on restore."""
    _build_qcow2(qcow, {0: _payload("d")},
                 [("broker-slot-10", {1: _payload("v")}, 123456)])
    trimmed = broker._trim_hdd_image("broker-slot-10")
    raw = trimmed.read_bytes()
    pos = int.from_bytes(raw[64:72], "big")
    assert struct.unpack(">QIHHIIQII", raw[pos:pos + 40])[7] == 123456


def test_trim_drops_the_other_slots_clusters(qcow):
    """The point of the exercise: slot 10 must not ship slots 1 and 3."""
    big = {i: _payload(f"slot1-{i}") for i in range(40)}
    _build_qcow2(qcow, {0: _payload("disk")},
                 [("broker-slot-1", big, 4096),
                  ("broker-slot-3", dict(big), 4096),
                  ("broker-slot-10", {0: _payload("keep")}, 4096)])
    before = qcow.stat().st_size
    trimmed = broker._trim_hdd_image("broker-slot-10")
    assert trimmed.stat().st_size < before / 2
    assert b"slot1-39" not in trimmed.read_bytes()


def test_trim_drops_leaked_clusters(qcow):
    """Clusters an interrupted snapshot job stranded are not carried over."""
    _build_qcow2(qcow, {0: _payload("disk")},
                 [("broker-slot-10", {1: _payload("vm")}, 4096)], leaked=20)
    trimmed = broker._trim_hdd_image("broker-slot-10")
    assert b"stranded by an interrupted job" not in trimmed.read_bytes()


def test_trim_preserves_sharing_between_disk_and_snapshot(qcow):
    """A snapshot sharing the active tables must not be expanded into a copy."""
    active = {i: _payload(f"d{i}") for i in range(50)}
    _build_qcow2(qcow, active, [("broker-slot-10", None, 4096)])
    trimmed = broker._trim_hdd_image("broker-slot-10")
    assert _read_guest(trimmed) == active
    assert _read_guest(trimmed, "broker-slot-10") == active
    # Shared, not duplicated: one copy of the payload, not two.
    assert trimmed.read_bytes().count(_payload("d49")) == 1


def test_trim_refuses_an_unknown_tag(qcow):
    _build_qcow2(qcow, {0: _payload("d")},
                 [("broker-slot-1", {1: _payload("v")}, 4096)])
    assert broker._trim_hdd_image("broker-slot-9") is None


def test_trim_refuses_compressed_clusters(qcow):
    """Compressed descriptors encode the offset differently; do not guess."""
    _build_qcow2(qcow, {0: _payload("d"), 5: _payload("e")},
                 [("broker-slot-10", {1: _payload("v")}, 4096)], compressed=True)
    assert broker._trim_hdd_image("broker-slot-10") is None


def test_trim_refuses_a_non_qcow2(qcow):
    qcow.write_bytes(b"not an image at all")
    assert broker._trim_hdd_image("broker-slot-10") is None


def test_trim_leaves_no_temporary_behind(qcow):
    _build_qcow2(qcow, {0: _payload("d")},
                 [("broker-slot-10", {1: _payload("v")}, 4096)])
    broker._trim_hdd_image("broker-slot-9")   # refused
    assert not list(qcow.parent.glob(".*.trim"))


def test_zip_hdd_image_ships_the_trimmed_copy(qcow):
    _build_qcow2(qcow, {0: _payload("disk")},
                 [("broker-slot-1", {i: _payload(f"a{i}") for i in range(40)}, 4096),
                  ("broker-slot-10", {0: _payload("keep")}, 4096)])
    content = _zip_members(broker._zip_hdd_image("broker-slot-10"))
    assert b"a39" not in content["xbox_hdd.qcow2"]
    assert not list(qcow.parent.glob(".*.trim"))


def test_zip_hdd_image_falls_back_to_the_whole_image(qcow):
    """A refused trim must still produce a usable archive."""
    _build_qcow2(qcow, {0: _payload("disk")},
                 [("broker-slot-1", {1: _payload("v")}, 4096)])
    content = _zip_members(broker._zip_hdd_image("broker-slot-9"))
    assert content["xbox_hdd.qcow2"] == qcow.read_bytes()


def test_zip_hdd_image_without_a_tag_ships_the_whole_image(qcow):
    _build_qcow2(qcow, {0: _payload("disk")},
                 [("broker-slot-10", {1: _payload("v")}, 4096)])
    content = _zip_members(broker._zip_hdd_image())
    assert content["xbox_hdd.qcow2"] == qcow.read_bytes()


def test_zip_hdd_image_honours_state_trim_off(qcow, monkeypatch):
    monkeypatch.setattr(broker, "STATE_TRIM", False)
    _build_qcow2(qcow, {0: _payload("disk")},
                 [("broker-slot-1", {1: _payload("v")}, 4096),
                  ("broker-slot-10", {2: _payload("w")}, 4096)])
    content = _zip_members(broker._zip_hdd_image("broker-slot-10"))
    assert content["xbox_hdd.qcow2"] == qcow.read_bytes()


# ── State frames ──────────────────────────────────────────────────────────────


def _png(width=1, height=1, pixel=b"\xff\x00\x00"):
    """A real, minimal PNG, so the tests assert on bytes RomM would accept."""
    def chunk(kind, data):
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return len(data).to_bytes(4, "big") + kind + data + crc.to_bytes(4, "big")

    raw = (b"\x00" + pixel * width) * height
    return (
        broker._PNG_MAGIC
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def cu_server(monkeypatch):
    """Stand in for pixelflux's Computer Use server on loopback.

    A real socket rather than a patched urlopen: posting the wrong action or
    hitting the wrong path is exactly the failure worth catching, and neither
    shows up when the transport itself is mocked away."""
    state = {
        "payload": {"data": base64.b64encode(_png()).decode()},
        "requests": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append((self.path, json.loads(body)))
            blob = json.dumps(state["payload"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(broker, "CU_PORT", str(srv.server_address[1]))
    yield state
    srv.shutdown()
    srv.server_close()


def test_capture_frame_png_returns_the_decoded_image(cu_server):
    assert broker._capture_frame_png() == _png()


def test_capture_frame_png_asks_for_a_screenshot(cu_server):
    broker._capture_frame_png()
    assert cu_server["requests"] == [("/computer-use", {"action": "screenshot"})]


def test_capture_frame_png_none_without_a_port(monkeypatch):
    """No PIXELFLUX_CU means no capture server, so nothing is attempted."""
    monkeypatch.setattr(broker, "CU_PORT", "")
    assert broker._capture_frame_png() is None


def test_capture_frame_png_none_when_nothing_is_listening(monkeypatch):
    """Base images predating Computer Use ignore PIXELFLUX_CU, so the port is
    set but refuses the connection. That must degrade to no thumbnail."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    monkeypatch.setattr(broker, "CU_PORT", str(port))
    assert broker._capture_frame_png() is None


def test_capture_frame_png_none_when_the_reply_has_no_image(cu_server):
    cu_server["payload"] = {"result": "ok"}
    assert broker._capture_frame_png() is None


def test_capture_frame_png_none_when_the_image_is_not_base64(cu_server):
    cu_server["payload"] = {"data": "not base64 !!"}
    assert broker._capture_frame_png() is None


def test_capture_frame_png_none_when_the_image_is_not_a_png(cu_server):
    """RomM drops a non-PNG, so a wrong format is caught before it is stored."""
    cu_server["payload"] = {"data": base64.b64encode(b"GIF89a").decode()}
    assert broker._capture_frame_png() is None


@pytest.fixture
def shots(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "STATE_SHOT_DIR", tmp_path / "shots")
    return tmp_path / "shots"


def test_capture_state_shot_writes_a_png(shots, cu_server):
    assert broker._capture_state_shot(4) is True
    assert (shots / "state-slot-4.png").read_bytes() == _png()


def test_capture_state_shot_false_when_the_capture_fails(shots, monkeypatch):
    monkeypatch.setattr(broker, "_capture_frame_png", lambda: None)
    assert broker._capture_state_shot(4) is False
    assert not (shots / "state-slot-4.png").exists()


def test_capture_state_shot_leaves_no_partial_file(shots, cu_server):
    """The frame is written via a temp name, so a reader never sees a half PNG."""
    broker._capture_state_shot(4)
    assert sorted(p.name for p in shots.iterdir()) == ["state-slot-4.png"]


def test_save_state_replaces_the_frame(shots, monkeypatch):
    """The capture belongs to the save that just happened, not the one before."""
    shots.mkdir()
    (shots / "state-slot-2.png").write_bytes(b"stale")
    monkeypatch.setattr(broker, "_qmp_snapshot", lambda cmd, tag: True)
    monkeypatch.setattr(broker, "_capture_state_shot", lambda s: shots.joinpath(
        f"state-slot-{s}.png").write_bytes(b"fresh") or True)
    assert broker._qmp_save_state(2) is True
    assert (shots / "state-slot-2.png").read_bytes() == b"fresh"


def test_save_state_drops_a_stale_frame_when_capture_fails(shots, monkeypatch):
    """Better no thumbnail than the previous save's picture on the new state."""
    shots.mkdir()
    (shots / "state-slot-2.png").write_bytes(b"stale")
    monkeypatch.setattr(broker, "_qmp_snapshot", lambda cmd, tag: True)
    monkeypatch.setattr(broker, "_capture_state_shot", lambda s: False)
    assert broker._qmp_save_state(2) is True
    assert not (shots / "state-slot-2.png").exists()


def test_failed_save_leaves_the_frame_alone(shots, monkeypatch):
    """The old snapshot survives a failed save, so its frame must too."""
    shots.mkdir()
    (shots / "state-slot-2.png").write_bytes(b"previous")
    monkeypatch.setattr(broker, "_qmp_snapshot", lambda cmd, tag: cmd != "snapshot-save")
    monkeypatch.setattr(
        broker, "_capture_state_shot", lambda s: pytest.fail("no save, no capture")
    )
    assert broker._qmp_save_state(2) is False
    assert (shots / "state-slot-2.png").read_bytes() == b"previous"


def test_restore_clears_every_frame(shots, hdd):
    """The frames picture snapshots in the image the restore just overwrote."""
    shots.mkdir()
    for slot in (1, 7):
        (shots / f"state-slot-{slot}.png").write_bytes(b"old session")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(broker.HDD_IMAGE_ENTRY, b"restored image")
    assert broker._restore_hdd_image(buf.getvalue()) is None
    assert list(shots.glob("state-slot-*.png")) == []


def test_failed_restore_keeps_the_frames(shots, hdd):
    """The disk was not replaced, so the frames still match it."""
    shots.mkdir()
    (shots / "state-slot-1.png").write_bytes(b"current")
    assert broker._restore_hdd_image(b"not a zip") is not None
    assert (shots / "state-slot-1.png").read_bytes() == b"current"


# ── GET /state-screenshot ─────────────────────────────────────────────────────


def test_get_state_screenshot_serves_the_frame(client, shots):
    shots.mkdir()
    (shots / "state-slot-6.png").write_bytes(broker._PNG_MAGIC + b"body")
    code, headers, body = _raw_req(client, "GET", "/state-screenshot?slot=6")
    assert code == 200
    assert headers["Content-Type"] == "image/png"
    assert body == broker._PNG_MAGIC + b"body"


def test_get_state_screenshot_404_when_no_frame(client, shots):
    """RomM treats the 404 as "this broker keeps no frames" and moves on."""
    assert _raw_req(client, "GET", "/state-screenshot?slot=6")[0] == 404


def test_get_state_screenshot_rejects_bad_slot(client, shots):
    assert _raw_req(client, "GET", "/state-screenshot?slot=abc")[0] == 400
    assert _raw_req(client, "GET", "/state-screenshot?slot=99")[0] == 400


def test_get_state_screenshot_works_with_xemu_down(client, shots, monkeypatch):
    """The whole point: /save-and-exit has killed xemu by the time RomM asks."""
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    shots.mkdir()
    (shots / "state-slot-6.png").write_bytes(broker._PNG_MAGIC)
    assert _raw_req(client, "GET", "/state-screenshot?slot=6")[0] == 200


# ── GET /state-file with xemu down ────────────────────────────────────────────


@pytest.fixture
def offline_client(client, tmp_path, monkeypatch):
    """xemu gone and a real qcow2 on disk, which is the /save-and-exit aftermath."""
    path = tmp_path / "xemu" / "xbox_hdd.qcow2"
    path.parent.mkdir(parents=True)
    _qcow2(path, ["broker-slot-3"])
    monkeypatch.setattr(broker, "HDD_IMAGE", path)
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "STATE_GET_WAIT", 0.3)
    monkeypatch.setattr(
        broker, "_qmp_snapshot_tags", lambda: pytest.fail("xemu is down; do not ask it")
    )
    return client


def test_get_state_file_serves_an_exit_save(offline_client):
    """save-and-exit kills xemu before RomM pulls, so requiring live QMP here
    stranded every exit save inside the container."""
    code, headers, body = _raw_req(offline_client, "GET", "/state-file?slot=3")
    assert code == 200
    assert headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert zf.read(broker.HDD_IMAGE_ENTRY)[:4] == b"QFI\xfb"


def test_get_state_file_offline_does_not_pause(offline_client, monkeypatch):
    """There is no guest to stop, and pausing a dead xemu would fail the read."""
    monkeypatch.setattr(broker, "_qmp_pause", lambda: pytest.fail("nothing to pause"))
    monkeypatch.setattr(broker, "_qmp_resume", lambda: pytest.fail("nothing to resume"))
    code, headers, _ = _raw_req(offline_client, "GET", "/state-file?slot=3")
    assert code == 200
    assert "X-Xemu-Paused" not in headers


def test_get_state_file_serves_an_image_bigger_than_the_transfer_limit(
    offline_client, monkeypatch
):
    """A qcow2 holding one ~70MB VM state runs past the 256MB transfer limit
    while zipping to well under it, so gating the image on that limit rejected
    saves that transfer fine. Compressible padding stands in for the VM state."""
    monkeypatch.setattr(broker, "STATE_FILE_MAX_BYTES", 4096)
    with broker.HDD_IMAGE.open("ab") as fh:
        fh.write(b"\0" * 200_000)
    assert broker.HDD_IMAGE.stat().st_size > broker.STATE_FILE_MAX_BYTES
    code, _, body = _raw_req(offline_client, "GET", "/state-file?slot=3")
    assert code == 200
    assert len(body) <= broker.STATE_FILE_MAX_BYTES  # the archive still fits


def test_get_state_file_413_when_the_image_could_never_be_restored(
    offline_client, monkeypatch
):
    """Past the expanded bound, PUT would refuse the member on the way back."""
    monkeypatch.setattr(broker, "HDD_IMAGE_MAX_BYTES", 8)
    assert _raw_req(offline_client, "GET", "/state-file?slot=3")[0] == 413


def test_get_state_file_413_when_the_archive_itself_is_too_big(
    offline_client, monkeypatch
):
    """The expanded image passed, but what goes over the wire still has to fit."""
    monkeypatch.setattr(broker, "STATE_FILE_MAX_BYTES", 8)
    code, _, body = _raw_req(offline_client, "GET", "/state-file?slot=3")
    assert code == 413
    assert json.loads(body)["error"] == "state file exceeds size limit"


def test_get_state_file_offline_404_when_slot_empty(offline_client):
    code, _, body = _raw_req(offline_client, "GET", "/state-file?slot=5")
    assert code == 404
    assert json.loads(body)["error"] == "no state for slot"


def test_get_state_file_offline_503_when_the_image_is_unreadable(offline_client):
    """An unparseable image must not read as "this slot is empty" — that would
    tell RomM the save vanished when the truth is the broker cannot tell."""
    broker.HDD_IMAGE.write_bytes(b"shredded" * 16)
    code, _, body = _raw_req(offline_client, "GET", "/state-file?slot=3")
    assert code == 503
    assert json.loads(body)["error"] == "could not read saved states from the disk image"


def test_get_state_file_trims_to_the_slot_it_serves(offline_client, monkeypatch):
    """Serving slot 3 must not ship the other slots' VM state along with it."""
    asked = []
    monkeypatch.setattr(broker, "_zip_hdd_image",
                        lambda keep_tag=None: asked.append(keep_tag) or b"PK\x03\x04")
    code, _, _ = _raw_req(offline_client, "GET", "/state-file?slot=3")
    assert code == 200
    assert asked == ["broker-slot-3"]


def test_get_state_file_409_when_xemu_restarts_mid_read(offline_client, monkeypatch):
    """A /launch landing during the zip reopens the image, so half the archive
    predates the reopen and the whole thing is untrustworthy."""
    monkeypatch.setattr(broker, "_zip_hdd_image", lambda keep_tag=None: (
        monkeypatch.setattr(broker, "_qmp_available", lambda: True), b"PK\x03\x04"
    )[1])
    code, _, body = _raw_req(offline_client, "GET", "/state-file?slot=3")
    assert code == 409
    assert "started" in json.loads(body)["error"]
    assert _wait_state_file_idle()


# ── DELETE /launch ────────────────────────────────────────────────────────────


def _wait_launch_idle(timeout=5.0):
    """Block until the background launch thread has run its finally block."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with broker._lock:
            if not broker._state["launch_in_progress"]:
                return True
        time.sleep(0.05)
    return False


def test_delete_launch_beats_an_in_flight_launch(client, rom_root, monkeypatch):
    """Stopping is the escape hatch from a hung launch: it is never refused, and
    the launch it interrupted must not write its session back afterwards."""
    booting = threading.Event()
    release = threading.Event()

    def _slow_wait_ready(timeout):
        booting.set()
        release.wait(timeout=10)
        return True

    monkeypatch.setattr(broker, "_qmp_wait_ready", _slow_wait_ready)
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)
    rom = rom_root / "xbox" / "game.iso"
    rom.write_bytes(b"iso")

    assert _req(client, "POST", "/launch", {"rom_path": str(rom)})[0] == 200
    assert booting.wait(timeout=5)
    assert _req(client, "DELETE", "/launch")[0] == 200  # not a 409
    release.set()
    assert _wait_launch_idle()

    with broker._lock:
        assert broker._state["rom_path"] is None
        assert broker._state["rom_name"] is None
        assert broker._state["started_at"] is None
    assert _status(client)["active"] is False
    # The stale rom_path used to block every later setup session with a 409.
    assert _req(client, "POST", "/setup")[0] == 200


def test_delete_launch_waits_out_a_state_file_transfer(state_client, monkeypatch):
    """The transfer holds xemu paused mid-read, so the stop lets it finish."""
    kills = []
    entered = threading.Event()
    release = threading.Event()

    def _slow_zip(keep_tag=None):
        entered.set()
        release.wait(timeout=10)
        return b"PK\x03\x04 pretend zip"

    monkeypatch.setattr(broker, "_zip_hdd_image", _slow_zip)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: kills.append("kill"))
    monkeypatch.setattr(broker, "STOP_WAIT", 5.0)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"

    get_result = {}
    getter = threading.Thread(target=lambda: get_result.update(
        resp=_raw_req(state_client, "GET", "/state-file?slot=3")
    ))
    stop_result = {}
    stopper = threading.Thread(target=lambda: stop_result.update(
        resp=_req(state_client, "DELETE", "/launch")
    ))
    getter.start()
    try:
        assert entered.wait(timeout=5)
        stopper.start()
        time.sleep(0.3)
        assert kills == []       # xemu is not killed out from under the read
        assert not stop_result   # the stop is still waiting
    finally:
        release.set()
        getter.join(timeout=10)
        stopper.join(timeout=10)

    code, headers, _ = get_result["resp"]
    assert code == 200
    assert "X-Xemu-Paused" not in headers
    assert stop_result["resp"][0] == 200
    assert kills == ["kill"]  # but the stop still wins once the read is done
    with broker._lock:
        assert broker._state["rom_path"] is None


def test_delete_launch_waits_out_a_save(client, monkeypatch):
    """A stop landing mid-save used to SIGTERM QEMU during snapshot-save."""
    kills = []
    entered = threading.Event()
    release = threading.Event()

    def _slow_save(slot):
        entered.set()
        release.wait(timeout=10)
        return True

    monkeypatch.setattr(broker, "_qmp_save_state", _slow_save)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: kills.append("kill"))
    monkeypatch.setattr(broker, "STOP_WAIT", 5.0)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"

    assert _req(client, "POST", "/save-state", {"slot": 3})[0] == 200
    assert entered.wait(timeout=5)
    stop_result = {}
    stopper = threading.Thread(target=lambda: stop_result.update(
        resp=_req(client, "DELETE", "/launch")
    ))
    stopper.start()
    try:
        time.sleep(0.3)
        assert kills == []       # the snapshot job is not cut in half
        assert not stop_result   # the stop is still waiting
    finally:
        release.set()
        stopper.join(timeout=10)
    assert stop_result["resp"][0] == 200
    assert kills == ["kill"]  # but the stop still wins once the save is done


def test_delete_launch_stops_anyway_once_the_window_runs_out(client, monkeypatch):
    """Stopping is the only way out of a wedged save, so it is never refused."""
    kills = []
    monkeypatch.setattr(broker, "_kill_xemu", lambda: kills.append("kill"))
    monkeypatch.setattr(broker, "STOP_WAIT", 0.3)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
        broker._state["save_in_progress"] = True  # a save that never concludes
    assert _req(client, "DELETE", "/launch")[0] == 200
    assert kills == ["kill"]


def test_get_state_file_does_not_call_a_killed_xemu_paused(state_client, monkeypatch):
    """A stop mid-read leaves nothing to resume — that is not "stayed paused"."""
    alive = [True]
    monkeypatch.setattr(broker, "_qmp_available", lambda: alive[0])
    monkeypatch.setattr(broker, "_qmp_resume", lambda: False)

    def _zip_then_stopped(keep_tag=None):
        alive[0] = False  # a DELETE /launch killed xemu during the read
        return b"PK\x03\x04 pretend zip"

    monkeypatch.setattr(broker, "_zip_hdd_image", _zip_then_stopped)
    code, headers, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 200
    assert "X-Xemu-Paused" not in headers
    assert len(body) > 0


def test_delete_launch_clears_session_errors(client):
    """Errors belong to the session that was just ended on purpose."""
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
        broker._state["launch_error"] = "xemu QMP not available — ROM load aborted"
        broker._state["resume_error"] = "no usable state in slot 3 — booted fresh"
    assert _req(client, "DELETE", "/launch")[0] == 200
    st = _status(client)
    assert st["launch_error"] is None
    assert st["resume_error"] is None


def test_delete_launch_beats_the_setup_commit(client, monkeypatch):
    """A stop landing after the supersede check must not leave a stale started_at
    or a watchdog armed for a session that was already torn down."""
    real_abandon = broker._abandon_if_cancelled

    def _stop_inside_the_commit_window(generation, spawned):
        # The check passes, then the stop lands before the commit runs — the
        # exact window the commit block has to re-close under the lock.
        result = real_abandon(generation, spawned)
        _req(client, "DELETE", "/launch")
        return result

    monkeypatch.setattr(broker, "_abandon_if_cancelled", _stop_inside_the_commit_window)

    assert _req(client, "POST", "/setup")[0] == 200
    assert _wait_launch_idle()

    with broker._lock:
        assert broker._state["setup"] is False
        assert broker._state["started_at"] is None
        assert broker._state["setup_timer"] is None  # no orphaned watchdog
    st = _status(client)
    assert st["setup"] is False
    assert st["active"] is False


# ── Snapshot job exclusion ────────────────────────────────────────────────────


def test_save_and_load_state_409_while_a_launch_is_in_flight(client):
    """A launch can be mid-resume, which is a snapshot job of its own."""
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
        broker._state["launch_in_progress"] = True
    code, body = _req(client, "POST", "/save-state", {"slot": 1})
    assert code == 409
    assert body["error"] == "launch in progress"
    code, body = _req(client, "POST", "/load-state", {"slot": 1})
    assert code == 409
    assert body["error"] == "launch in progress"


def test_save_state_cannot_run_alongside_a_slot_resume(client, rom_root, monkeypatch):
    """POST /save-state during a resume-from-slot launch used to run
    snapshot-save concurrently with snapshot-load against one vmstate."""
    entered = threading.Event()
    release = threading.Event()
    saves = []

    def _slow_resume(slot):
        entered.set()
        release.wait(timeout=10)
        return True

    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)
    monkeypatch.setattr(broker, "_qmp_load_state", _slow_resume)
    monkeypatch.setattr(broker, "_qmp_save_state", lambda s: saves.append(s) or True)
    rom = rom_root / "xbox" / "game.iso"
    rom.write_bytes(b"iso")
    # A session is already on record, so "no game is running" is not what turns
    # the save away — the exclusion flags are.
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/previous.iso"

    assert _req(client, "POST", "/launch",
                {"rom_path": str(rom), "load_slot": 3})[0] == 200
    assert entered.wait(timeout=5)
    try:
        assert _req(client, "POST", "/save-state", {"slot": 1})[0] == 409
        assert _req(client, "POST", "/load-state", {"slot": 1})[0] == 409
    finally:
        release.set()
    assert _wait_launch_idle()
    assert saves == []  # no second snapshot job ran against the resume

    # The refusal was for the duration of the resume only, not a permanent pin.
    assert _req(client, "POST", "/save-state", {"slot": 1})[0] == 200


def test_save_and_exit_supersedes_an_in_flight_launch(client, rom_root, monkeypatch):
    """Exiting ends the session, so the launch it interrupted must not write its
    rom_path back afterwards over a session the user already left."""
    booting = threading.Event()
    release = threading.Event()

    def _slow_wait_ready(timeout):
        booting.set()
        release.wait(timeout=10)
        return True

    monkeypatch.setattr(broker, "_qmp_wait_ready", _slow_wait_ready)
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)
    monkeypatch.setattr(broker, "_qmp_save_state", lambda s: True)
    rom = rom_root / "xbox" / "game.iso"
    rom.write_bytes(b"iso")
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/previous.iso"
        broker._state["rom_name"] = "previous"

    assert _req(client, "POST", "/launch", {"rom_path": str(rom)})[0] == 200
    assert booting.wait(timeout=5)
    code, body = _req(client, "POST", "/save-and-exit", {"slot": 5})
    assert code == 200
    assert body["saved"] is True
    release.set()
    assert _wait_launch_idle()

    with broker._lock:
        assert broker._state["rom_path"] is None
        assert broker._state["rom_name"] is None
        assert broker._state["started_at"] is None
    assert _status(client)["active"] is False


def test_save_and_exit_clears_session_errors(client, monkeypatch):
    """The session ended cleanly — /status must not keep reporting its errors."""
    monkeypatch.setattr(broker, "_qmp_save_state", lambda s: True)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
        broker._state["launch_error"] = "xemu QMP not available — ROM load aborted"
        broker._state["resume_error"] = "no usable state in slot 3 — booted fresh"
    assert _req(client, "POST", "/save-and-exit", {"slot": 3})[0] == 200
    st = _status(client)
    assert st["launch_error"] is None
    assert st["resume_error"] is None


# ── Stalled clients ───────────────────────────────────────────────────────────


def _stalled_request(base, method, path, secret=None):
    """Announce a body that never arrives; returns the still-open socket.

    This is the client that used to pin an exclusion flag until restart: the
    flag was claimed, and only then was the body read."""
    port = int(base.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: 127.0.0.1",
        "Content-Type: application/json",
        "Content-Length: 64",
    ]
    if secret is not None:
        headers.append(f"X-Broker-Secret: {secret}")
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
    return sock


def _drain_stalled(sock):
    """Half-close and read the handler out, so it is not left writing a
    response into a socket the test already closed."""
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(5)
        while sock.recv(4096):
            pass
    except OSError:
        pass
    finally:
        sock.close()


@pytest.mark.parametrize("path", ["/save-state", "/load-state", "/save-and-exit"])
def test_stalled_body_does_not_pin_the_save_flag(client, monkeypatch, path):
    monkeypatch.setattr(broker, "_qmp_save_state", lambda s: True)
    monkeypatch.setattr(broker, "_qmp_load_state", lambda s: True)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
    sock = _stalled_request(client, "POST", path)
    try:
        time.sleep(0.4)
        with broker._lock:
            assert broker._state["save_in_progress"] is False
        # And every later caller still gets served rather than a blanket 409.
        assert _req(client, "POST", "/save-state", {"slot": 1})[0] == 200
    finally:
        _drain_stalled(sock)


def test_stalled_body_does_not_pin_the_state_file_flag(restore_client, hdd):
    sock = _stalled_request(restore_client, "PUT", "/state-file?filename=a.x01")
    try:
        time.sleep(0.4)
        with broker._lock:
            assert broker._state["state_file_in_progress"] is False
        code, _, _ = _raw_req(
            restore_client, "PUT", "/state-file?filename=b.x02",
            _state_archive(b"pushed"),
        )
        assert code == 200
        assert hdd.read_bytes() == b"pushed"
    finally:
        _drain_stalled(sock)


def test_handler_has_a_request_timeout():
    """Without one, a client that stalls owns a handler thread forever."""
    assert broker.BrokerHandler.timeout is not None
    assert broker.BrokerHandler.timeout > 0


def test_request_timeout_drops_a_stalled_client(client, monkeypatch):
    monkeypatch.setattr(broker.BrokerHandler, "timeout", 0.3)
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
    sock = _stalled_request(client, "POST", "/save-state")
    try:
        sock.settimeout(5)
        assert sock.recv(4096) == b""  # the handler gave up instead of blocking
    finally:
        sock.close()


# ── Shared-secret auth ────────────────────────────────────────────────────────

_SECRET = "correct-horse-battery-staple"

# Everything that changes state. /health is deliberately absent: container
# healthchecks cannot carry the secret.
_MUTATING = [
    ("POST", "/launch", {"rom_path": "/romm/library/x.iso"}),
    ("POST", "/setup", None),
    ("POST", "/save-state", {"slot": 1}),
    ("POST", "/load-state", {"slot": 1}),
    ("POST", "/save-and-exit", {"slot": 1}),
    ("POST", "/cleanup", None),
    ("POST", "/volume", {"level": 50}),
    ("POST", "/mute", {"mute": True}),
    ("DELETE", "/launch", None),
]


@pytest.fixture
def secret_client(client, monkeypatch):
    """The same server with BROKER_SECRET actually configured."""
    monkeypatch.setattr(broker, "SECRET", _SECRET)
    return client


@pytest.mark.parametrize("method,path,body", _MUTATING)
def test_mutating_endpoints_reject_a_missing_secret(secret_client, method, path, body):
    code, resp = _req(secret_client, method, path, body)
    assert code == 403
    assert resp["error"] == "forbidden"


@pytest.mark.parametrize("method,path,body", _MUTATING)
def test_mutating_endpoints_reject_a_wrong_secret(secret_client, method, path, body):
    assert _req(secret_client, method, path, body, secret=_SECRET + "x")[0] == 403
    assert _req(secret_client, method, path, body, secret="")[0] == 403


def test_a_rejected_delete_does_not_end_the_session(secret_client):
    """403 must be a refusal, not a refusal after the fact."""
    with broker._lock:
        broker._state["rom_path"] = "/romm/library/x.iso"
    assert _req(secret_client, "DELETE", "/launch", secret="nope")[0] == 403
    with broker._lock:
        assert broker._state["rom_path"] == "/romm/library/x.iso"


def test_health_stays_open_without_a_secret(secret_client):
    assert _req(secret_client, "GET", "/health")[0] == 200


def test_get_endpoints_require_the_secret(secret_client):
    assert _req(secret_client, "GET", "/status")[0] == 403
    assert _req(secret_client, "GET", "/status", secret="wrong")[0] == 403
    assert _req(secret_client, "GET", "/status", secret=_SECRET)[0] == 200
    assert _raw_req(secret_client, "GET", "/state-file?slot=3")[0] == 403


def test_put_state_file_requires_the_secret(secret_client, hdd, monkeypatch):
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    payload = _state_archive(b"pushed disk image")
    path = "/state-file?filename=a.x01"
    assert _raw_req(secret_client, "PUT", path, payload)[0] == 403
    assert _raw_req(secret_client, "PUT", path, payload, secret="wrong")[0] == 403
    assert hdd.read_bytes() == b"original image"  # nothing was written
    assert _raw_req(secret_client, "PUT", path, payload, secret=_SECRET)[0] == 200
    assert hdd.read_bytes() == b"pushed disk image"


def test_correct_secret_reaches_the_endpoint(secret_client, rom_root, monkeypatch):
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: True)
    rom = rom_root / "xbox" / "game.iso"
    rom.write_bytes(b"iso")
    code, body = _req(
        secret_client, "POST", "/launch", {"rom_path": str(rom)}, secret=_SECRET
    )
    assert code == 200
    assert body["status"] == "loading"
    assert _wait_launch_idle()


def test_secret_comparison_is_constant_time(secret_client, monkeypatch):
    """A plain == leaks the secret one byte at a time to a timing attacker."""
    seen = []
    real = broker.hmac.compare_digest
    monkeypatch.setattr(
        broker.hmac, "compare_digest",
        lambda a, b: seen.append((a, b)) or real(a, b),
    )
    assert _req(secret_client, "GET", "/status", secret=_SECRET)[0] == 200
    assert seen and seen[0] == (_SECRET, _SECRET)


def test_no_secret_configured_leaves_everything_open(client):
    """The default deployment has no secret; requests must not start 403ing."""
    assert _req(client, "GET", "/status")[0] == 200
    assert _req(client, "GET", "/status", secret="anything at all")[0] == 200


# ── Audio ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def pactl_calls(monkeypatch):
    """Record pactl invocations instead of talking to PulseAudio."""
    calls = []

    def _record(*args):
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 0, "no", "")

    monkeypatch.setattr(broker, "_pactl", _record)
    return calls


def test_mute_rejects_a_non_boolean(client, pactl_calls):
    """The JSON string "false" is truthy — taking it at face value mutes the
    session the caller asked to unmute."""
    code, body = _req(client, "POST", "/mute", {"mute": "false"})
    assert code == 400
    assert "boolean" in body["error"]
    assert pactl_calls == []  # nothing was sent to the sink


def test_mute_accepts_booleans(client, pactl_calls):
    code, _ = _req(client, "POST", "/mute", {"mute": False})
    assert code == 200
    assert pactl_calls[0] == ("set-sink-mute", "@DEFAULT_SINK@", "0")
    assert _req(client, "POST", "/mute", {"mute": True})[0] == 200
    assert pactl_calls[2] == ("set-sink-mute", "@DEFAULT_SINK@", "1")


def test_mute_without_a_body_toggles(client, pactl_calls):
    code, _ = _req(client, "POST", "/mute")
    assert code == 200
    assert pactl_calls[0] == ("set-sink-mute", "@DEFAULT_SINK@", "toggle")
