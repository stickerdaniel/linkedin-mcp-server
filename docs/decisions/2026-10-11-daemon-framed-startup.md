# Start the daemon owner over framed standard input and output

- Date: 2026-10-11
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Start the shared-browser daemon from a clean slate](2026-10-11-daemon-clean-slate.md), only its "One startup protocol" entry, which kept the loopback control channel until this stage
- Status: decided; `daemon_enabled` stays `False` until the release gates of the 2026-09-26 contract pass

## Decision

A frontend and the owner it spawns talk only over the child's standard input and output. There is no loopback control channel.

- **Frames.** Each message is a 4-byte big-endian length followed by 1 to 65,536 bytes of strict UTF-8 JSON naming exactly the fields of its type. The sequence is HELLO, ACK, CONFIG, PREPARED, COMMIT, VERDICT, each bound to the post-spawn nonce. A malformed, oversized or truncated frame, or end of input, ends the start; neither side resynchronises.
- **Protocol number.** HELLO names startup protocol 5, defined once as `PROTOCOL` in `linkedin_mcp_server/daemon/startup.py`, and an owner answers nothing to any other value. The number continues after the line records of released frontends (1 to 3) and the record of the clean-slate stage (4), and is never reused. No released owner can parse a frame, so the framing itself is the fence across that boundary.
- **Build identity before configuration.** The configuration, which carries the proxy password, is written only after the owner's ACK names this protocol and the frontend's own build identity: distribution, version and a SHA-256 over the package's files, frozen when the frontend first elects. A mismatch ends the start before CONFIG and the frontend falls back to Direct. A check of the installed build before spawning is a diagnostic only; the ACK comparison is the authority, because a version alone does not identify code in editable or same-version installs.
- **Termination authority.** The frontend may kill the child until it writes COMMIT and never after. COMMIT names the prepared instance, and the owner checks both the instance and the nonce.
- **Clean output.** At the start of its entry point the owner moves its standard input and output to private descriptors and points the originals at the null device and standard error, so later output cannot reach the channel. Its first write on the channel is a nonce-bearing sync line, and framing starts after it, so output from imports or startup hooks before that point cannot be read as a frame.

## Why

The loopback channel was a second rendezvous that existed to keep startup protocols of released owners working. With no compatibility kept, one duplex pipe the frontend already owns carries the whole exchange, needs no listening socket, and lets an owner that does not speak the protocol be killed before it ever sees the configuration.

## Evidence

Measured on 2026-10-11 before implementation, with length-prefixed frames through the real `process_gate` and a named Job on `windows-latest` (8 passed) and on macOS (178 fault cases, zero publications before commit, zero kills after commit). Linux shares the POSIX pipe semantics of macOS and is covered by the ordinary CI suite.
