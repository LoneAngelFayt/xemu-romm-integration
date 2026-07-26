"""GPU environment forwarding: what survives the sudo hop into xemu.

sudo's env_reset wipes the container environment, so a renderer setting the
operator put in docker-compose only reaches xemu if the broker names it on the
`env` command line. Everything unnamed is silently dropped, which reads from
the outside like the container ignoring its own configuration.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "root" / "root"))
import broker  # noqa: E402


def test_forwards_vendor_graphics_variables(monkeypatch):
    monkeypatch.setenv("VK_DRIVER_FILES", "/usr/share/vulkan/icd.d/nvidia_icd.json")
    monkeypatch.setenv("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
    monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
    monkeypatch.setenv("LIBGL_ALWAYS_SOFTWARE", "0")
    env = broker._gpu_env()
    assert env["VK_DRIVER_FILES"] == "/usr/share/vulkan/icd.d/nvidia_icd.json"
    assert env["__GLX_VENDOR_LIBRARY_NAME"] == "nvidia"
    assert env["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "0"


def test_forwards_the_base_image_render_node_selector(monkeypatch):
    """DRINODE has no DRI_ prefix, so it needs naming explicitly."""
    monkeypatch.setenv("DRINODE", "/dev/dri/renderD130")
    assert broker._gpu_env()["DRINODE"] == "/dev/dri/renderD130"


def test_ignores_unrelated_variables(monkeypatch):
    monkeypatch.setenv("BROKER_SECRET", "hunter2")
    env = broker._gpu_env()
    assert "BROKER_SECRET" not in env
    assert "PATH" not in env


def test_empty_values_are_skipped(monkeypatch):
    """`env VAR=` is set-but-empty rather than unset, and consumers disagree on
    what that means, so an empty value is not worth forwarding."""
    monkeypatch.setenv("DRI_NODE", "")
    assert "DRI_NODE" not in broker._gpu_env()


def test_display_passthrough_still_conditional(monkeypatch):
    """The image exports DISPLAY=:1 while only X0 exists, so DISPLAY is only
    set when the base image provided one. The GPU merge must not change that."""
    if os.environ.get("DISPLAY"):
        assert broker.ENV["DISPLAY"] == os.environ["DISPLAY"]
    else:
        assert "DISPLAY" not in broker.ENV
