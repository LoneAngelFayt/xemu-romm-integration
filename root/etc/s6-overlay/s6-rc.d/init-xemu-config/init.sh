#!/usr/bin/with-contenv bash

# ── XDG runtime dir ───────────────────────────────────────────────────────────
XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"

# Clean up stale Wayland and X11 sockets so pixelflux/Xwayland always start on
# the default indices (wayland-1, :0).  Stale lock files on the host-mapped
# /config volume cause them to increment on relaunch, breaking the broker's
# hardcoded display expectations.
find "$XDG_RUNTIME_DIR" -name "wayland-*" -delete
rm -rf /tmp/.X11-unix/X* /tmp/.X*lock
echo "[xemu-broker-mod] Cleaned up stale display sockets."

# ── python3 availability ─────────────────────────────────────────────────────
_need_apt=0
command -v python3 &>/dev/null || _need_apt=1
if [ "$_need_apt" = "1" ]; then
    echo "[xemu-broker-mod] Installing missing packages (python3)..."
    apt-get update -qq && apt-get install -y -qq python3 \
        || echo "[xemu-broker-mod] ERROR: apt-get install failed"
fi

# ── Disable labwc autostart ───────────────────────────────────────────────────
# Temporarily disabled for no-broker controller test.
# AUTOSTART="/config/.config/labwc/autostart"
# mkdir -p "$(dirname "$AUTOSTART")"
# printf '# Disabled by xemu-broker-mod\n' > "$AUTOSTART"
# echo "[xemu-broker-mod] Disabled labwc autostart."

# ── Seed xemu.toml defaults ──────────────────────────────────────────────────
# xemu stores its config at $HOME/.local/share/xemu/xemu/xemu.toml.
# We seed two things:
#   [input.bindings]   port1_driver = 'usb-xbox-gamepad'  — always, so a
#                      fresh container presents port 1 as an SDL gamepad
#                      without requiring manual UI setup.
#   [display]          renderer = 'Vulkan'                 — only on AMD GPUs,
#                      to avoid known bug
# Keys are only written if not already present so user edits are preserved.
XEMU_CONFIG="/config/.local/share/xemu/xemu/xemu.toml"

_amd_gpu=0
grep -q '^amdgpu ' /proc/modules 2>/dev/null && _amd_gpu=1

if [ "$_amd_gpu" = "1" ]; then
    echo "[xemu-broker-mod] AMD GPU detected — will seed Vulkan renderer."
else
    echo "[xemu-broker-mod] No AMD GPU detected — skipping Vulkan renderer seed."
fi

python3 - "$XEMU_CONFIG" "$_amd_gpu" <<'PYEOF'
import sys, re
from pathlib import Path

p = Path(sys.argv[1])
amd_gpu = sys.argv[2] == '1'

p.parent.mkdir(parents=True, exist_ok=True)
text = p.read_text() if p.exists() else ''

def _seed(txt, section, key, value):
    """Add key=value under section if key is not already present anywhere."""
    if re.search(rf'^\s*{re.escape(key)}\s*=', txt, re.MULTILINE):
        return txt, False  # already set, preserve user value
    section_pat = rf'(^{re.escape(section)}[^\n]*\n)'
    if re.search(section_pat, txt, re.MULTILINE):
        txt = re.sub(section_pat, rf'\g<1>{key} = {value}\n', txt, count=1, flags=re.MULTILINE)
    else:
        txt += f'\n{section}\n{key} = {value}\n'
    return txt, True

changed = False

text, did = _seed(text, '[input.bindings]', 'port1_driver', "'usb-xbox-gamepad'")
if did:
    print("[xemu-broker-mod] Seeded [input.bindings] port1_driver = 'usb-xbox-gamepad'.")

if amd_gpu:
    text, did = _seed(text, '[display]', 'renderer', "'Vulkan'")
    if did:
        print("[xemu-broker-mod] Seeded [display] renderer = 'Vulkan'.")
    else:
        print("[xemu-broker-mod] [display] renderer already set — skipping.")

p.write_text(text)
PYEOF

# ── Input device name diagnostic (DEBUG only) ────────────────────────────────
if [ "${BROKER_LOG_LEVEL,,}" = "debug" ]; then
    echo "[xemu-broker-mod] Input device names (for SDL controller mapping):"
    for node in js0 js1 js2 js3; do
        name_file="/sys/class/input/${node}/device/name"
        if [ -f "$name_file" ]; then
            echo "[xemu-broker-mod]   /dev/input/${node}: $(cat "$name_file")"
        else
            echo "[xemu-broker-mod]   /dev/input/${node}: sysfs name not found"
        fi
    done
fi
