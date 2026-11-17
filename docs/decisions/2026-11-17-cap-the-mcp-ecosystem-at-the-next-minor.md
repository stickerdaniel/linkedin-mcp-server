# Cap FastMCP, MCP and httpx2 at the next minor

- Date: 2026-11-17
- Supersedes in part: [2026-10-12 daemon core and forwarder](./2026-10-12-daemon-core-and-forwarder.md), its sentence on what `forwarder-compat.yml` tests

## Decision

- `pyproject.toml` declares `fastmcp`, `mcp` and `httpx2` from the locked version up to the next minor, such as `fastmcp>=4.2.0,<4.3.0`. `tests/test_fastmcp_compatibility.py` fails on a range that admits the next minor.
- A new minor arrives through a Renovate pull request. Renovate's `pep621` manager has no lockfile-only update, so `rangeStrategy` `auto` resolves to `replace`, and `replace` turns `>=4.2.0,<4.3.0` into `>=4.3.0,<4.4.0` when 4.3.0 appears (Renovate `lib/modules/manager/index.ts` `getRangeStrategy`, `lib/modules/versioning/pep440/index.spec.ts`). No `packageRule` is needed.
- `forwarder-compat.yml` runs on that pull request through its `pyproject.toml` and `uv.lock` paths, against the newest releases the declared ranges allow. On the daily schedule it also runs against the newest releases below the next major, which names a breaking minor before its Renovate pull request opens.

## Why

`uvx` resolves the declared ranges, not `uv.lock`. With `fastmcp>=4.1.7,<5`, FastMCP 4.2.0 (2026-11-13) reached users untested and broke `resources/list` on a frontend whose owner had stopped; #1513 fixed it after `forwarder-compat.yml` failed on 2026-11-14 at 06:28 UTC. Under the same ranges Renovate opened no pull request for any of the three packages, because every release fell inside them: on 2026-11-17 `uv.lock` held httpx2 2.13.1 while `uvx` installed 2.16.2.

`mcp` and `httpx2` share the cap because the daemon's owner hop uses both directly: the forwarder subclasses an SDK `ClientSession` and httpx2's transport and byte-stream classes, and FastMCP declares both only below their next major.

## Rejected alternative

- **Cap FastMCP alone.** FastMCP 4.2.0 requires `mcp<3` and `httpx2>=2.5.0`, so a FastMCP cap leaves new MCP and httpx2 minors reaching the owner hop untested.

## Known limit

A patch inside the cap still reaches users before the lock moves, because `replace` keeps an in-range range unchanged and Renovate then opens no pull request. The daily `declared` run of `forwarder-compat.yml` tests such a patch within a day of its release.
