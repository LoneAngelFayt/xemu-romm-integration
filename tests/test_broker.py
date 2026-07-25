"""Tests for the xemu broker, focused on the /setup session lifecycle.

The broker is a single stdlib module under root/root; import it directly and
exercise the state machine plus the HTTP contract against a real (but xemu-less)
server, with the process/QMP helpers mocked out.
"""

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
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
    monkeypatch.setattr(broker, "_launch_xemu", lambda: True)
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
    monkeypatch.setattr(broker, "_launch_xemu", lambda: False)
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
    monkeypatch.setattr(broker, "_launch_xemu", lambda: True)
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
        broker, "_launch_xemu", lambda: pytest.fail("a live instance must be reused")
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


def test_do_load_rom_qmp_timeout_kills_the_xemu_it_spawned(monkeypatch):
    """A discless xemu left behind busy-loops CPU cores with nothing to reap it."""
    calls = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda: True)
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
        broker, "_launch_xemu", lambda: pytest.fail("a live instance must be reused")
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


def test_do_load_rom_kills_the_xemu_it_spawned_when_the_rom_never_loads(monkeypatch):
    """A spawned xemu whose disc never went in has no session at all, and a
    gameless one busy-loops CPU cores with nothing left to reap it."""
    calls = []
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    monkeypatch.setattr(broker, "_launch_xemu", lambda: True)
    monkeypatch.setattr(broker, "_qmp_wait_ready", lambda t: True)
    monkeypatch.setattr(broker, "_qmp_load_rom", lambda p: False)
    monkeypatch.setattr(broker, "_kill_xemu", lambda: calls.append("kill"))
    with broker._lock:
        broker._state["launch_in_progress"] = True
    broker._do_load_rom("/romm/library/x.iso")
    assert calls == ["kill"]
    with broker._lock:
        assert broker._state["launch_error"]
        assert broker._state["rom_path"] is None
        assert broker._state["launch_in_progress"] is False


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
    monkeypatch.setattr(broker, "_launch_xemu", lambda: True)
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
    monkeypatch.setattr(broker, "STATE_FILE_MAX_BYTES", 128)
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


def test_get_state_file_serves_zipped_image(state_client, hdd):
    code, headers, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 200
    assert headers["Content-Type"] == "application/zip"
    assert headers["X-State-Filename"] == "xemu.x03"
    assert "X-Xemu-Paused" not in headers
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert zf.read(broker.HDD_IMAGE_ENTRY) == b"original image"
    with broker._lock:
        assert broker._state["state_file_in_progress"] is False


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
    with broker._lock:
        assert broker._state["state_file_in_progress"] is False


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
    with broker._lock:
        assert broker._state["state_file_in_progress"] is False


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


def test_get_state_file_409_when_xemu_down(state_client, monkeypatch):
    monkeypatch.setattr(broker, "_qmp_available", lambda: False)
    assert _raw_req(state_client, "GET", "/state-file?slot=3")[0] == 409


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

    def _slow_zip():
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
    with broker._lock:
        assert broker._state["state_file_in_progress"] is False


def test_put_state_file_restores_image(restore_client, hdd):
    payload = b"pushed disk image" * 32
    code, _, body = _raw_req(
        restore_client, "PUT", "/state-file?filename=Halo.x03", _state_archive(payload)
    )
    assert code == 200
    assert json.loads(body)["filename"] == "Halo.x03"
    assert hdd.read_bytes() == payload
    with broker._lock:
        assert broker._state["state_file_in_progress"] is False


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
    with broker._lock:
        assert broker._state["state_file_in_progress"] is False


def test_put_state_file_409_while_xemu_runs(client, hdd):
    code, _, _ = _raw_req(
        client, "PUT", "/state-file?filename=Halo.x03", _state_archive()
    )
    assert code == 409


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


def test_get_state_file_413_without_zipping_an_oversized_image(state_client, hdd, monkeypatch):
    """The archive is built in memory, so the limit is enforced before zipping."""
    zipped = []
    paused = []
    monkeypatch.setattr(broker, "_zip_hdd_image", lambda: zipped.append(1))
    monkeypatch.setattr(broker, "_qmp_pause", lambda: paused.append(1) or True)
    monkeypatch.setattr(broker, "STATE_FILE_MAX_BYTES", 4)  # hdd holds 14 bytes
    code, _, body = _raw_req(state_client, "GET", "/state-file?slot=3")
    assert code == 413
    assert json.loads(body)["error"] == "state file exceeds size limit"
    assert zipped == []  # no compressed copy was ever built
    assert paused == []  # and the guest was never stopped for it
    with broker._lock:
        assert broker._state["state_file_in_progress"] is False


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

    def _slow_zip():
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


def test_get_state_file_does_not_call_a_killed_xemu_paused(state_client, monkeypatch):
    """A stop mid-read leaves nothing to resume — that is not "stayed paused"."""
    alive = [True]
    monkeypatch.setattr(broker, "_qmp_available", lambda: alive[0])
    monkeypatch.setattr(broker, "_qmp_resume", lambda: False)

    def _zip_then_stopped():
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
