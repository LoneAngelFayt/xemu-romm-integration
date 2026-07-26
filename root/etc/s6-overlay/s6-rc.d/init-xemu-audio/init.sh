#!/usr/bin/with-contenv bash

# ── PulseAudio null sinks ────────────────────────────────────────────────────
# selkies captures the game's sound from `output.monitor`, the monitor of a null
# sink the base image creates from svc-selkies. That setup block waits only for
# pulseaudio's pid file to appear, which happens before the daemon accepts
# connections, so its first `pactl load-module` can come back "Connection
# refused" while the daemon is still working through default.pa. Nothing checks
# for that failure and the block still touches /dev/shm/audio.lock on its way
# out, so the `output` sink stays missing for the life of the container and the
# stream is silent. Losing the race is a coin flip, which is why the same image
# has sound on one start and none on the next.
#
# svc-selkies depends on this script, so we get to do that setup first and do it
# properly: wait until the daemon actually answers, create whichever sinks are
# absent, then claim the lock so the racy block finds nothing left to do. If the
# daemon never answers we leave the lock alone and the base image behaves
# exactly as it does today — no worse off than without the mod.
#
# Every pactl call must run as abc, never as root. The client library calls
# pa_make_secure_dir() on PULSE_RUNTIME_PATH and takes ownership of it, so a
# single root pactl chowns /defaults to root:root 0700 and every later abc
# client — selkies, pcmflux, the broker's volume control — is locked out of the
# socket for the life of the container. Audio dies quietly and the daemon looks
# perfectly healthy from a root shell.

export PULSE_RUNTIME_PATH="${PULSE_RUNTIME_PATH:-/defaults}"

AUDIO_LOCK="/dev/shm/audio.lock"
READY_TIMEOUT=15

pa() {
    s6-setuidgid abc with-contenv pactl "$@"
}

ready=""
deadline=$((SECONDS + READY_TIMEOUT))
while [ "$SECONDS" -lt "$deadline" ]; do
    if pa info >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.2
done

if [ -z "$ready" ]; then
    echo "[xemu-broker-mod] PulseAudio did not answer within ${READY_TIMEOUT}s — leaving sink setup to the base image."
else
    for sink in output input; do
        if pa list short sinks 2>/dev/null | cut -f2 | grep -qx "$sink"; then
            continue
        fi
        if pa load-module module-null-sink \
                sink_name="$sink" \
                sink_properties=device.description="$sink" >/dev/null 2>&1; then
            echo "[xemu-broker-mod] Created missing PulseAudio null sink '${sink}'."
        else
            echo "[xemu-broker-mod] WARNING: could not create PulseAudio null sink '${sink}'."
        fi
    done

    # selkies reads from output.monitor, so the game has to be playing into
    # `output`. Whichever sink lands first would otherwise become the default.
    pa set-default-sink output >/dev/null 2>&1

    have_both=1
    for sink in output input; do
        pa list short sinks 2>/dev/null | cut -f2 | grep -qx "$sink" || have_both=""
    done

    if [ -n "$have_both" ]; then
        touch "$AUDIO_LOCK"
    else
        echo "[xemu-broker-mod] WARNING: PulseAudio sinks incomplete — letting the base image retry."
    fi
fi

exit 0
