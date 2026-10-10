#!/bin/sh
# Container entrypoint: sanity-check the desktop plumbing the compose file is
# supposed to provide, then exec the app. Problems are reported in plain words
# because with `docker compose up -d` the only place anyone sees them is
# `docker compose logs`.
set -eu

say() { printf 'nin8: %s\n' "$*" >&2; }

# GTK/WebKit want a private, 0700 runtime dir.
mkdir -p "${XDG_RUNTIME_DIR}"
chmod 0700 "${XDG_RUNTIME_DIR}"

# --- X11 (native X, or XWayland on a Wayland session) -----------------------
case "${DISPLAY:-}" in
    '')
        say "DISPLAY is not set - run 'docker compose up' from your desktop session."
        exit 1
        ;;
    :*)
        num="${DISPLAY#:}"
        num="${num%%.*}"
        if [ ! -S "/tmp/.X11-unix/X${num}" ]; then
            say "no X11 socket for DISPLAY=${DISPLAY} (/tmp/.X11-unix/X${num})."
            say "Is the host's X11/XWayland running, and DISPLAY the same one the host uses (echo \$DISPLAY)?"
            exit 1
        fi
        ;;
esac

if [ -n "${XAUTHORITY:-}" ] && [ ! -r "${XAUTHORITY}" ]; then
    say "cannot read the X authority file (${XAUTHORITY}). The container must run as your"
    say "host uid - run docker/setup.sh, or set NIN8_UID/NIN8_GID in .env."
fi

# --- Audio (PulseAudio protocol: PipeWire-pulse or PulseAudio on the host) --
if [ -n "${PULSE_SERVER:-}" ] && [ ! -S "${PULSE_SERVER#unix:}" ]; then
    say "PulseAudio socket ${PULSE_SERVER#unix:} not found - the synth will start without sound."
    say "Is pipewire-pulse (or pulseaudio) running on the host?"
fi

exec "$@"
