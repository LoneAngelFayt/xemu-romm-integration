"""Tests for the xemu broker, focused on the /setup session lifecycle.

The broker is a single stdlib module under root/root; import it directly and
exercise the state machine plus the HTTP contract against a real (but xemu-less)
server, with the process/QMP helpers mocked out.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
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
            "launch_error": None,
            "setup": False,
            "setup_timer": None,
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


def _req(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
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
