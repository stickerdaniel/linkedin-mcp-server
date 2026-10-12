# Decide the daemon in pure cores and forward through public APIs

- Date: 2026-10-12
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Status: decided; `daemon_enabled` stays `False` until the release gates of the 2026-09-26 contract pass

## Decision

The daemon's decisions live in pure reducers, and the code that carries them out talks to FastMCP and the MCP SDK only through their public names.

- **Cores.** Each module in `linkedin_mcp_server/daemon/core` is one reducer, `on_event(state, event) -> (state, effects)`: `call` for a forwarded call's binding, dispatch and deadline, `owner` for the owner's admission, retirement, heartbeats and drain, `lookup`, `spawn` and `election` for choosing and starting an owner. Several small reducers, not one state machine.
- **Shells.** Everything that awaits, reads a clock, starts a process or touches a file or socket stays outside `core` and executes the effects a reducer names. Time and identifiers reach a reducer as event values, and each completion names the operation it answers, so a late completion of an abandoned operation changes nothing.
- **Boundary guard.** `core/ruff.toml` bans the obvious ways into I/O and every adapter module beside the core; `tests/test_daemon_core_purity.py` reads the adapter list off the package, so a new adapter fails until it is banned. A guard against accident, not a proof of purity.
- **Owner binding stays in the shell.** Refusing a call to a written-off owner is a core decision; capturing and retaining which owner a call is bound to stays in the proxy middleware.
- **Public-API forwarding.** The frontend forwards through a FastMCP `Provider` and an SDK `ClientSession` subclass and reads no private name of either library; `tests/test_daemon_forwarder.py` checks the whole daemon package for that. `.github/workflows/forwarder-compat.yml` runs the forwarder daily against the newest FastMCP and MCP releases the declared ranges allow, because `uvx` resolves them fresh on every install.
- **Stateless hop.** The frontend-to-owner hop uses MCP protocol 2026-07-28, which needs no server-sent event stream. sse-starlette sets a process-wide exit flag after any uvicorn shutdown, and that flag ended later event streams in the same process.

## Why

Decisions interleaved with I/O could only be tested through processes, sockets and timing, and races in them were found late. A reducer runs every ordering in a unit test. A forwarder on private library names breaks on a release the lock never saw, which a default-on `uvx` install reaches first.

## Evidence

Each stage compared the extracted reducer against the code it replaced. The election stage matched on 1,224 lookup cases, 2,500 election traces and 126 startup cases, reviewed on 2026-10-12. The owner reducer's schedule test runs 6,720 orderings of admission, completion, expiry, stand-down, cutoff and idle ticks.
