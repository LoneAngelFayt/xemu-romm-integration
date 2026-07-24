#!/usr/bin/with-contenv bash

# ── XDG runtime dir ───────────────────────────────────────────────────────────
XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"

# Clean up stale Wayland and X11 sockets so pixelflux/Xwayland always start on
# the default indices. Stale lock files on the host-mapped /config volume cause
# them to increment on relaunch, breaking hardcoded display expectations.
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

# ── Disable boot-time xemu in the desktop autostart ──────────────────────────
# The base image autostart runs: xterm -e /opt/xemu/AppRun
# The broker owns the xemu lifecycle (spawns it on /launch with the -qmp flag,
# kills it when the session ends), so a boot-time instance would fight the
# broker for the QMP socket and busy-loop CPU cores idling at the dashboard.
# Written for both the labwc and openbox image variants; the broker-managed
# marker keeps manual edits from being clobbered on restart.
for AUTOSTART in /config/.config/labwc/autostart /config/.config/openbox/autostart; do
    mkdir -p "$(dirname "$AUTOSTART")"
    if [ ! -f "$AUTOSTART" ] || ! grep -q "broker-managed" "$AUTOSTART"; then
        printf '#!/bin/bash\n\n# xemu is broker-managed: the RomM broker launches it on demand with a\n# QMP socket and kills it when the session ends. Do not launch it here.\n' > "$AUTOSTART"
        echo "[xemu-broker-mod] Wrote broker-managed autostart at $AUTOSTART."
    else
        echo "[xemu-broker-mod] $AUTOSTART already broker-managed — skipping."
    fi
done

# ── Seed xemu.toml defaults ──────────────────────────────────────────────────
# xemu stores its config at $HOME/.local/share/xemu/xemu/xemu.toml.
# We seed two things:
#   [input.bindings]   port1_driver = 'usb-xbox-gamepad'  — always, so a
#                      fresh container presents port 1 as an SDL gamepad
#                      without requiring manual UI setup.
#   [display]          renderer = 'VULKAN'                 — only on AMD GPUs.
#                      xemu's OpenGL path asserts in gl_fence after an amdgpu
#                      ring-timeout reset on Renoir/Mesa, so pin Vulkan, which
#                      is the renderer the other emulators run stably here.
#                      xemu's toml enum tokens are upper-case: 'OPENGL',
#                      'VULKAN', 'NULL' (a mixed-case 'Vulkan' is rejected).
# Keys are only written if not already present so user edits are preserved.
XEMU_CONFIG="/config/.local/share/xemu/xemu/xemu.toml"

_amd_gpu=0
grep -q '^amdgpu ' /proc/modules 2>/dev/null && _amd_gpu=1

if [ "$_amd_gpu" = "1" ]; then
    echo "[xemu-broker-mod] AMD GPU detected — will pin Vulkan renderer."
else
    echo "[xemu-broker-mod] No AMD GPU detected — skipping renderer seed."
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
        return txt, False
    section_pat = rf'(^{re.escape(section)}[^\n]*\n)'
    if re.search(section_pat, txt, re.MULTILINE):
        txt = re.sub(section_pat, rf'\g<1>{key} = {value}\n', txt, count=1, flags=re.MULTILINE)
    else:
        txt += f'\n{section}\n{key} = {value}\n'
    return txt, True

text, did = _seed(text, '[input.bindings]', 'port1_driver', "'usb-xbox-gamepad'")
if did:
    print("[xemu-broker-mod] Seeded [input.bindings] port1_driver = 'usb-xbox-gamepad'.")

if amd_gpu:
    # Pin Vulkan: the OpenGL path hangs the amdgpu GPU on this stack. Force any
    # non-Vulkan value to 'VULKAN', else seed it on first run. Enum tokens are
    # upper-case ('OPENGL'/'VULKAN'/'NULL'); xemu writes 'OPENGL' when a user
    # picks it in the UI, so match the existing value regardless of case.
    m_r = re.search(r"^(\s*renderer\s*=\s*)'([^']*)'", text, re.MULTILINE)
    if m_r:
        if m_r.group(2) != 'VULKAN':
            text = f"{text[:m_r.start()]}{m_r.group(1)}'VULKAN'{text[m_r.end():]}"
            print(f"[xemu-broker-mod] Set [display] renderer to 'VULKAN' (was '{m_r.group(2)}').")
        else:
            print("[xemu-broker-mod] [display] renderer already 'VULKAN'.")
    else:
        text, _ = _seed(text, '[display]', 'renderer', "'VULKAN'")
        print("[xemu-broker-mod] Seeded [display] renderer = 'VULKAN'.")

p.write_text(text)
PYEOF

# ── Per-container Xbox hard disk image ───────────────────────────────────────
# xemu writes save-state snapshots INTO the hard disk qcow2. The stock image
# normally sits on a shared bios mount, so every container would write its
# snapshots into the same file and players would see each other's states. Copy
# it into /config once and point xemu at the copy.
HDD_LOCAL="/config/xemu/xbox_hdd.qcow2"
HDD_STOCK="${HDD_STOCK:-/config/bios/Xbox Hard Disk Image/xbox_hdd.qcow2}"

python3 - "$XEMU_CONFIG" "$HDD_LOCAL" "$HDD_STOCK" <<'PYEOF'
import os, re, shutil, sys
from pathlib import Path

config, local, stock = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
text = config.read_text() if config.exists() else ''

m = re.search(r"^\s*hdd_path\s*=\s*'([^']*)'", text, re.MULTILINE)
current = m.group(1) if m else ''


def usable(p):
    """A qcow2 xemu can actually open. A truncated copy is worse than none:
    xemu falls back to its first-run wizard and the container looks unset up."""
    try:
        if p.stat().st_size == 0:
            return False
        with p.open('rb') as fh:
            return fh.read(4) == b'QFI\xfb'
    except OSError:
        return False


# hdd_path is rewritten to the local copy on first run, so it stops being a
# usable source. Keep the stock image as the fallback origin.
source = Path(current) if current and Path(current) != local else stock

if usable(local):
    print('[xemu-broker-mod] Hard disk image already container-local.')
else:
    if local.exists():
        print(f'[xemu-broker-mod] {local} is not a usable qcow2, recopying.')
    if not usable(source):
        print(f'[xemu-broker-mod] ERROR: no usable stock hard disk image at {source}, '
              'leaving hdd_path alone.')
        sys.exit(0)
    local.parent.mkdir(parents=True, exist_ok=True)
    # Copy via a temp name so an interrupted copy never lands on the real
    # path, where the next run would accept it as done.
    part = local.with_name(local.name + '.part')
    try:
        shutil.copy2(source, part)
        if part.stat().st_size != source.stat().st_size:
            raise OSError(f'short copy: {part.stat().st_size} of {source.stat().st_size} bytes')
        os.replace(part, local)
    except OSError as exc:
        part.unlink(missing_ok=True)
        print(f'[xemu-broker-mod] ERROR: copying {source} -> {local} failed: {exc}')
        sys.exit(0)
    print(f'[xemu-broker-mod] Copied hard disk image {source} -> {local}.')

if current == str(local):
    sys.exit(0)

if m:
    text = f'{text[:m.start(1)]}{local}{text[m.end(1):]}'
elif re.search(r'^\[sys\.files\]', text, re.MULTILINE):
    text = re.sub(r'(^\[sys\.files\][^\n]*\n)', f"\\g<1>hdd_path = '{local}'\n",
                  text, count=1, flags=re.MULTILINE)
else:
    text += f"\n[sys.files]\nhdd_path = '{local}'\n"

config.write_text(text)
print(f'[xemu-broker-mod] Pointed hdd_path at {local}.')
PYEOF

chown -R abc:abc /config/xemu 2>/dev/null || true

# ── Fix ownership so xemu (running as abc) can write its config ───────────────
chown -R abc:abc "$(dirname "$XEMU_CONFIG")" 2>/dev/null || true
echo "[xemu-broker-mod] Fixed xemu config dir ownership (abc:abc)."

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
