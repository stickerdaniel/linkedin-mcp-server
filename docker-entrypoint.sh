#!/bin/bash
set -euo pipefail

# Tini delivers SIGTERM to this supervisor, which stops Python before Xvfb so
# browser cleanup keeps a live display. `xvfb-run` is not a substitute: measured
# under `docker stop`, its EXIT trap cleaned up Xvfb but it never forwarded TERM
# to Python; the container exited 143 without the server's shutdown path.
: "${DISPLAY:=:99}"
export DISPLAY

# This container owns a local X server. A host prefix means a remote display,
# which Xvfb cannot create and `-nolisten tcp` could not serve. Accept the local
# X11 forms only, with an optional screen suffix.
if [[ $DISPLAY =~ ^:([0-9]+)(\.[0-9]+)?$ ]]; then
    display_number=${BASH_REMATCH[1]}
else
    printf 'DISPLAY must be a local X display such as :99 or :99.0, got %q\n' \
        "$DISPLAY" >&2
    exit 2
fi

socket_path="/tmp/.X11-unix/X${display_number}"
lock_path="/tmp/.X${display_number}-lock"

# A SIGKILL leaves both names in the container's writable layer. Docker restart
# reuses that layer, so accepting the old socket as readiness starts Python
# against a display that never came up and traps the container in a restart loop.
rm -f -- "$socket_path" "$lock_path"

Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!

# Starting Chromium against a display whose process exists but whose socket is
# not ready fails exactly like having no display. Bounded and with Xvfb's own log
# on failure, so a broken image exits with the useful error instead of hanging.
attempt=0
while [[ $attempt -lt 100 ]]; do
    if [[ -S "$socket_path" ]]; then
        break
    fi
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        wait "$xvfb_pid" || true
        cat /tmp/xvfb.log >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 0.1
done

if [[ ! -S "$socket_path" ]]; then
    kill -TERM "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" || true
    cat /tmp/xvfb.log >&2
    exit 1
fi

# A shell gives an asynchronous list /dev/null for stdin, but only in the
# absence of an explicit redirection. Backgrounding the server as a bare `"$@" &`
# therefore cut the stdio transport off from the container's stdin: it started,
# announced the transport, read EOF from /dev/null at once and shut down without
# ever answering. That shipped in 4.22.0 and made `docker run -i` return nothing.
# The server has to stay a child, because `wait -n -p` below needs both children
# to decide which death ends the container, so it gets the redirection spelled
# out rather than being moved into the foreground.
#
# Docker always supplies a readable descriptor 0, but a launcher need not, and
# what it hands over then travels straight into Python. A closed one arrives as
# EBADF on every read; an open directory kills the interpreter outright, before
# argument parsing, so `--transport streamable-http` and the one-shot commands
# die on an input none of them reads. The bare `&` used to hide both behind its
# /dev/null, which is the one thing it was good for.
#
# Both are cheap to recognise: duplicating the descriptor fails when it is
# closed, and /dev/fd names what it points at when it is not. Ask in a subshell,
# where a failed duplication costs nothing, and hand the unusable cases the
# /dev/null they would have had. Without /proc there is no /dev/fd and the
# directory test cannot answer, which leaves the descriptor as it was: the same
# state every Docker container is in anyway.
( exec 3<&0 && [ ! -d /dev/fd/3 ] ) 2>/dev/null || exec 0</dev/null
"$@" <&0 &
server_pid=$!

terminate() {
    trap - TERM INT HUP
    kill -TERM "$server_pid" 2>/dev/null || true

    # Keep the display alive until browser and viewer cleanup completes. Killing
    # Xvfb alongside Python can wedge Chromium teardown until Docker resorts to
    # SIGKILL, leaving the profile unrestored and the token file behind.
    server_status=0
    wait "$server_pid" || server_status=$?
    kill -TERM "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" || true
    exit "$server_status"
}
trap terminate TERM INT HUP

# `wait -n -p` is why this is Bash rather than POSIX sh. Whichever child dies
# first decides the container: if Xvfb disappears, stop the server so Docker's
# restart policy sees a failed container instead of a live MCP endpoint with no
# display; if the server exits, stop Xvfb and return the server's status.
set +e
wait -n -p first_child "$xvfb_pid" "$server_pid"
first_status=$?
set -e

if [[ $first_child == "$xvfb_pid" ]]; then
    kill -TERM "$server_pid" 2>/dev/null || true
    wait "$server_pid" || true
    cat /tmp/xvfb.log >&2
    [[ $first_status -ne 0 ]] || first_status=1
    exit "$first_status"
fi

kill -TERM "$xvfb_pid" 2>/dev/null || true
wait "$xvfb_pid" || true
exit "$first_status"
