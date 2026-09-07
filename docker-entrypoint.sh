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
# what it hands over then travels straight into Python. Three shapes are not
# input: a closed descriptor and a write-only one both arrive as EBADF on the
# first read, and an open directory kills the interpreter outright, before
# argument parsing, so `--transport streamable-http` and the one-shot commands
# die on an input none of them reads. The bare `&` used to hide all three behind
# its /dev/null, which is the one thing it was good for.
#
# Each is cheap to recognise. Duplicating the descriptor fails when it is
# closed, which is the one question /proc cannot answer, since a closed
# descriptor and an absent /proc look alike from the outside. /dev/fd names what
# an open one points at. /proc/self/fdinfo gives its open flags, and `-r` cannot
# stand in for those, because it asks about permissions on the target and
# answers yes for a write-only /dev/null. Where /proc cannot answer, the
# descriptor is left as it was: the state every Docker container is in anyway.
#
# The closed case does not arrive through the image's own ENTRYPOINT. Tini calls
# `tcsetpgrp(STDIN_FILENO, ...)` before executing anything and treats every
# errno but ENOTTY and ENXIO as fatal (`src/tini.c`), so a closed descriptor 0
# ends the container at Tini with `tcsetpgrp failed: Bad file descriptor` and
# status 1. It reaches this script when the entrypoint is overridden or the
# script is run directly, which is also how the tests reach it.
#
# Each test says so on its own rather than leaning on `set -e`. Inside a `&&` or
# `||` list errexit is suspended, so a failing `exec` there stops aborting and
# the checks after it read a descriptor that was never opened, which is how an
# earlier shape of this guard came to pass the closed case straight through.
stdin_is_unusable() {
    (exec 3<&0) 2>/dev/null || return 0
    [ ! -d /dev/fd/0 ] || return 0

    local flags
    flags=$(sed -n 's/^flags:[[:space:]]*//p' /proc/self/fdinfo/0 2>/dev/null) ||
        return 1
    [ -n "$flags" ] || return 1

    # O_PATH names a file without opening it. Every read fails, and the access
    # mode bits still read 0, so the mode alone would call it readable.
    if (( (8#$flags & 8#10000000) != 0 )); then
        return 0
    fi

    # Two of the four access modes cannot be read: 1 is write-only, and 3 is
    # the value Linux uses for an open that grants neither direction.
    case $(( 8#$flags & 3 )) in
    1 | 3) return 0 ;;
    esac
    return 1
}

if stdin_is_unusable; then
    exec 0</dev/null
fi

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
