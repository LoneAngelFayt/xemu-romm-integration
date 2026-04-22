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
# Prevents xemu from being launched a second time by the desktop session —
# the broker manages the process lifecycle directly.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by xemu-broker-mod\n' > "$AUTOSTART"
echo "[xemu-broker-mod] Disabled labwc autostart."

# ── Seed xemu.toml defaults ──────────────────────────────────────────────────
# xemu stores its config at $HOME/.local/share/xemu/xemu/xemu.toml.
# We seed two things:
#   [input.bindings]   port1_driver = 'usb-xbox-gamepad'  — always, so a
#                      fresh container presents port 1 as an SDL gamepad
#                      without requiring manual UI setup.
#   [display]          renderer = 'Vulkan'                 — only on AMD GPUs,
#                      where Vulkan outperforms OpenGL.
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

# ── Selkies input_handler.py patches ─────────────────────────────────────────
# Glob over the python version so patches survive base-image upgrades that bump
# e.g. python3.12 → python3.13.
INPUT_HANDLER=$(compgen -G "/lsiopy/lib/python3.*/site-packages/selkies/input_handler.py" | head -1)

if [ -z "$INPUT_HANDLER" ]; then
    echo "[xemu-broker-mod] ERROR: selkies input_handler.py not found — Python version glob matched nothing."
    echo "[xemu-broker-mod]   Expected: /lsiopy/lib/python3.*/site-packages/selkies/input_handler.py"
    echo "[xemu-broker-mod]   Selkies patches will be skipped. Check base image Python version."
elif [ -f "$INPUT_HANDLER" ]; then
    # Patch 1: Active EOF detection in the keep-alive loop.
    #
    # The phase-2 keep-alive loop in _handle_interposer_client is:
    #
    #   while self.running and not writer.is_closing():
    #       await asyncio.sleep(0.1)
    #
    # writer.is_closing() never flips on Unix sockets when the remote end
    # closes, so dead emulator connections accumulate indefinitely.
    #
    # The naive fix (adding `not reader.at_eof()` to the while condition)
    # fails because at_eof() returns `self._eof AND not self._buffer`.
    # If the interposer has buffered any data at exit, _buffer is non-empty
    # and at_eof() stays False forever.
    #
    # The real fix: replace asyncio.sleep(0.1) with a short-timeout read.
    # reader.read(1) returns b"" on EOF regardless of buffer state, so we
    # detect emulator disconnect within one 0.1 s tick.
    if grep -q "wait_for(reader.read(1)" "$INPUT_HANDLER"; then
        echo "[xemu-broker-mod] selkies input_handler.py EOF patch already applied."
    else
        if python3 - "$INPUT_HANDLER" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
text = p.read_text()
# Handle both the original loop and any previously applied at_eof() variant.
variants = [
    '            while self.running and not writer.is_closing():\n                await asyncio.sleep(0.1) ',
    '            while self.running and not writer.is_closing():\n                await asyncio.sleep(0.1)',
    '            while self.running and not writer.is_closing() and not reader.at_eof():\n                await asyncio.sleep(0.1) ',
    '            while self.running and not writer.is_closing() and not reader.at_eof():\n                await asyncio.sleep(0.1)',
]
new_loop = (
    '            while self.running and not writer.is_closing():\n'
    '                try:\n'
    '                    _bdata = await asyncio.wait_for(reader.read(1), timeout=0.1)\n'
    '                    if not _bdata:\n'
    '                        break\n'
    '                except asyncio.TimeoutError:\n'
    '                    pass\n'
    '                except Exception:\n'
    '                    break'
)
for old in variants:
    if old in text:
        p.write_text(text.replace(old, new_loop, 1))
        sys.exit(0)
sys.exit(1)
PYEOF
        then
            echo "[xemu-broker-mod] Patched selkies input_handler.py keep-alive loop (active EOF detection)."
        else
            echo "[xemu-broker-mod] ERROR: python patch failed on input_handler.py keep-alive loop"
        fi
    fi

    # Patch 2: Silence the selkies_gamepad logger.
    # It emits ~80 INFO lines per launch cycle; demote to WARNING.
    if grep -q "setLevel(logging.WARNING)" "$INPUT_HANDLER"; then
        echo "[xemu-broker-mod] selkies_gamepad log-level patch already applied."
    else
        if python3 - "$INPUT_HANDLER" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
old = 'logger_selkies_gamepad = logging.getLogger("selkies_gamepad")'
new = old + '\nlogger_selkies_gamepad.setLevel(logging.WARNING)'
text = p.read_text()
if old in text:
    p.write_text(text.replace(old, new, 1))
    sys.exit(0)
sys.exit(1)
PYEOF
        then
            echo "[xemu-broker-mod] Patched selkies_gamepad log level to WARNING."
        else
            echo "[xemu-broker-mod] ERROR: python patch failed setting selkies_gamepad log level"
        fi
    fi
else
    echo "[xemu-broker-mod] WARNING: selkies input_handler.py not found at $INPUT_HANDLER"
fi

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
