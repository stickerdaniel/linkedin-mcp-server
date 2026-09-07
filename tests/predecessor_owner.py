"""An owner that speaks the protocol this package shipped before commit records.

Run as a process by ``TestPredecessorOwnerCompatibility``, never imported. The
two functions below are the predecessor's own, copied unchanged from
``linkedin_mcp_server/daemon_owner.py`` at ``a0d965cb`` (the commit before
``4b795a10`` introduced the prepare/commit boundary), because the property under
test is exactly what that code does and a paraphrase would prove nothing:

    def _read_handover() -> daemon_config.OwnerHandover:
        raw = sys.stdin.read()
        if not raw.strip():
            raise ValueError("The daemon was started without a configuration")
        return daemon_config.decode_handover(raw)

    def _write(self, message: str) -> None:
        stream.write(f"{HANDSHAKE} {self._nonce} {message}\\n")
        stream.flush()

``sys.stdin.read()`` is the whole of it. It returns when the frontend closes the
pipe and not before, so a frontend that holds the pipe open for its own commit
record never gets past this line, and this process never gets past it either.

The handover is read with :mod:`json` rather than through ``decode_handover``
because that function belongs to the *current* package: the point here is that
the record a current frontend writes is still readable by a decoder that has
never heard of a startup protocol or a control channel, which is a claim about
the record's shape rather than about today's parser.
"""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("The daemon was started without a configuration")
    handover = json.loads(raw)

    # What the predecessor's own decoder looked at, and nothing else. A record
    # that fails this would fail in the released owner too.
    if not {"browser", "server", "handshake_nonce"} <= set(handover):
        raise ValueError("The owner configuration is missing a section")
    nonce = handover["handshake_nonce"]

    # The predecessor publishes on its own authority and then says so. There is
    # no prepared generation and no commit record anywhere in this protocol.
    sys.stdout.write(f"owner {nonce} ready\n")
    sys.stdout.flush()

    # Owners outlive the frontend that started them. The test stops this one.
    time.sleep(600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
