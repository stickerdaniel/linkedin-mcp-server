# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

- Before changing repository knowledge, invariant comments, or temporary migration artifacts, read the [knowledge policy](docs/knowledge-policy.md).

## Development Commands

- Use `uv` for dependency management: `uv sync` (dev: `uv sync --group dev`)
- Lint: `uv run ruff check .` (auto-fix with `--fix`)
- Format: `uv run ruff format .`
- Type check: `uv run ty check` (using ty, not mypy)
- Tests: `uv run pytest` (with coverage: `uv run pytest --cov`)
- Pre-commit: `uv run pre-commit install` then `uv run pre-commit run --all-files`
- Run server locally: `uv run -m linkedin_mcp_server --no-headless`
- Run via uvx (PyPI/package verification only): `uvx mcp-server-linkedin`
- Docker build: `docker build -t linkedin-mcp-server .`
- Install browser: `uv run patchright install chromium`

## Scraping Rules

- **Voyager / private API.** Out of scope. [Read the rendered page](docs/decisions/2026-09-16-rendered-page.md).
- **One section = one navigation.** Each entry in `PERSON_SECTIONS` / `COMPANY_SECTIONS` (`scraping/fields.py`) maps to exactly one page navigation. Never combine multiple URLs behind a single section.
- **Minimize DOM dependence.** Prefer innerText and URL navigation over DOM selectors. When DOM access is unavoidable, use minimal generic selectors (`a[href*="/jobs/view/"]`) — never class names tied to LinkedIn's layout.
- **Detection must be locale-independent.** Classification logic — connection state, action availability, button identity — must rely on URL patterns (`/preload/custom-invite/?vanityName=USER`, `/in/USER/edit/intro/`, `/messaging/compose/`), attribute *presence* (`aria-label` exists, `aria-expanded` exists, `aria-disabled` exists), or structural counts — never on text values like "Connect", "Follow", "Message", "1st", "Pending". The verb in an `aria-label` is locale-dependent; whether the attribute exists is not. Where text is genuinely the only signal, guard it behind an explicit per-locale table and document the limitation in code.

## Browser Identity Rules

- **The browser must not contradict itself.** Anything it says about itself has
  to survive being checked against another surface of the same browser: the
  user-agent against `sec-ch-ua`, the page against its workers and iframes, the
  reported screen against the window sitting on it. The goal is coherence, not
  invisibility — invisibility cannot be proven, while a contradiction is a fact
  and can be found by anyone who looks twice.
- **Never inject a fingerprint.** No `user_agent`, no custom headers, no
  spoofed client hints. Patchright's own guidance says the same, and every
  override measured here made things worse: a `user_agent=` argument changes
  the string but not the client hints, and never reaches service workers at
  all ([playwright#5237](https://github.com/microsoft/playwright/issues/5237),
  closed as an upstream Chromium bug). A browser telling the truth beats one
  caught lying.
- **A proxy must contain every egress path, not just HTTP.** WebRTC uses UDP
  and went around a configured proxy until the switches in
  `browser_launch.py`. DNS and QUIC belong to the same family; check them
  before assuming a new setting is contained.
- **Verify identity changes by measurement.** `docs/browser-fingerprint.md`
  lists the four detectors, what each one alone would miss, and the values
  measured so far. A launch-configuration change without a measurement against
  them is a guess.

## Profile Safety Rules

- **Nothing is moved or deleted under a root the server cannot prove it owns.**
  Every operation that calls `rmtree`, `shutil.move`, `unlink` or `rename` on a
  user-supplied path goes through `_owned()` in `session_state.py`, which
  answers from `profile_claim.require_profile_claim`. Adding a destructive
  operation without routing it through that is the one change this rule exists
  to catch: `USER_DATA_DIR` accepts any path, and a mistyped one costs a
  directory nobody meant to name.
- **Guard the configured source root, once, before any short-circuit.** Not the
  derived paths, which are computed from a root that already passed, and not
  after an exists-or-empty check, which a foreign directory reaches without ever
  being judged. A check on a derived runtime profile is worse than none: it
  asks about a nested auth root the server deletes on purpose, while reading as
  protection.
- **The auth root is the blast radius, not the profile.** `cookies.json`,
  `source-state.json`, `runtime-profiles/` and every `invalid-state-*` live one
  level *above* `USER_DATA_DIR`, so the emptiness of the profile says nothing
  about what a rotation takes with it.
- **Expand and resolve together, always.** Doing one without the other lets a
  symlink move the profile out of one directory while its sidecars come from
  another. Use `session_state.canonical()`.
- **A browser older than the profile is refused, and only that direction.**
  `browser_downgrade.refuse_a_downgrade()` runs in `BrowserManager.start()`
  before anything is created or opened. Chromium does not stop a downgrade on
  macOS or Linux: it opens the profile and lets each store decide for itself,
  and a store that answers `INIT_TOO_NEW` is dropped in silence. Losing the
  cookie store that way looks exactly like an expired session. Every unknown
  here fails open (an unreadable marker, a binary that will not name itself),
  because not knowing is not evidence. That trade is one-shot per profile: the
  older browser then rewrites `Last Version` down to its own number, so the
  evidence is gone for good, which is why those two branches warn.
- **Never ask a Windows browser for its version.** Chromium compiles
  `HandleVersionSwitches` only under `BUILDFLAG(IS_POSIX)`, so there `--version`
  is an unrecognised switch and the binary starts a browser on whatever profile
  it defaults to. The guard is off on Windows on purpose; a version could only
  come from the executable's file-version resource.
- **Two versions only compare inside one product.** `Last Version` records a
  number and no product, so a comparison across products compares two
  numbering schemes: Vivaldi is on 7.x and Edge's build number sits an order
  of magnitude below Chrome's under the same major. Only the Chrome-family
  names in `_COMPARABLE_PRODUCTS` are compared, matched **whole and never as a
  prefix** — a prefix scan accepted a launcher script announcing itself as
  `Chromium launcher 1.2.3` and refused the newer browser behind it. And only
  for the *running* binary, which is all `--version` can identify. A profile
  written by a fork is therefore still refused; that one is not repairable from
  `Last Version`, and the error says so by naming the number to go back to
  rather than a browser.
- **Never trim `_COMPARABLE_PRODUCTS` to one name.** At the current lock two
  are live at once, on the same release: Playwright downloads its own Chromium
  build for Linux arm64 and Chrome for Testing everywhere else, so the
  published arm64 container reports `Chromium` while the amd64 one and macOS
  report `Google Chrome for Testing`, at the same revision. Dropping either
  entry turns the guard off for a shipped platform. That is the split *at the
  lock* and it does not hold across the whole supported range: at the declared
  floor every platform reports `Chromium`, and revision 1200 moved macOS and
  Linux x64 together, leaving only Linux arm64 behind. Which is the point:
  both managed names occur, and which one where depends on when and where, so
  neither is redundant. The third entry, `google chrome`, is not a managed
  browser at all but what an operator's own binary reports under `CHROME_PATH`,
  and it earns its place only when *that* Chrome is the older one: the guard
  reads the running binary, never the profile's writer. `browsers.json` is not
  evidence here: its `title` key dates from patchright 1.58.0 and omits the
  `Google` the binary prints. See `_COMPARABLE_PRODUCTS` for the measurements.

## Extension Bundle Rules

- **An optional `user_config` field needs a `default`.** A host substitutes
  `${user_config.NAME}` from the manifest's defaults plus the answers the user
  gave; a field in neither is not in that map, so the placeholder is handed to
  the server verbatim as if it were a setting. Measured in Claude Desktop's own
  substitution routine. `required: true` is the other safe shape, because a
  host skips the whole MCP config while a required field is empty.
  `tests/test_manifest.py` holds this line; `mcpb validate` does not, and
  cannot: the schema knows nothing about substitution.
- **What counts as a sufficient default depends on where the placeholder
  sits.** In a string, which is where the four `env` mappings sit, `""` is
  enough: it substitutes to nothing and the loader reads an empty variable as
  unset. As an entire element of `args` it is not, because that substitution
  is guarded by a truthiness test on the replacement and `""` is falsy, so the
  element keeps its literal. An array-valued default reached from a string is
  refused outright and also keeps the literal. Measured; the test knows all
  three.
- **A placeholder that does reach the process is not a value.** `_env()` in
  `config/loaders.py` drops it. Both directions matter and only one is loud:
  `PROXY_SERVER` fails validation and stops the server, while
  `PROXY_USERNAME` is offered to the proxy as a credential and comes back as a
  timeout that reads like an expired session.

## Tool Return Format

All scraping tools return: `{url, sections: {name: raw_text}}`.

Optional additional keys:

- `references: {section_name: [{kind, url, text?, context?, value?}]}` — LinkedIn URLs are relative paths; `value` carries non-URL identifiers (e.g. company URN id for `kind: "company_urn"`)
- `section_errors: {section_name: {error_type, error_message, issue_template_path, runtime, ...}}`
- `unknown_sections: [name, ...]`
- `job_ids: [id, ...]` (search_jobs and get_saved_jobs)
- `total: {count, exact}` (search_jobs only) — the result count LinkedIn advertises on the first page; `exact` is false for a lower bound such as "500+"
- `promoted_job_ids: [id, ...]` (search_jobs only) — the subset of `job_ids` shown as promoted; present only when every page could be read, so an empty list means none were
- `references["feed"]` (get_feed only) — every entry is `kind: "feed_post"`; non-post anchors (sidebar profiles, employer logos) are filtered. URLs may carry either `/feed/update/<urn>/` (DOM-anchor-derived) or `/posts/<slug>` (SDUI-derived) form; both are valid LinkedIn permalinks. Cap is 50 entries, matching `get_feed`'s `num_posts` ceiling.
- `references["search_results"]` (search_posts only) — DOM references first, then up to 50 `kind: "feed_post"` permalinks read from the page's JSON/document payload responses (`/feed/update/<urn>/` or `/posts/<slug>`, both valid). Captured permalinks are appended, not aligned to result order.

## Tests

- **Tautologies.** Assert an observable contract independent of the
  implementation. Before committing a test, mutate the covered behaviour
  to introduce a plausible regression and watch that test fail. Reject
  language restatements and redundant assertions. Pin an always-loaded
  instruction pointer. A sentence in a disclosed doc is not a test.
  For scroll-stop tests, let one batch land per round so a premature
  stop fails the test.
- **Browser-DOM tests belong where the unit suite mocks `page.evaluate`.**
  Extractor JS never executes under a mock, so a `browser_dom` test is its
  only coverage. Prefer a unit test elsewhere, and keep in mind that a
  fixture imitating LinkedIn's markup is a claim about LinkedIn, while one
  driving a synthetic container is a claim about the algorithm only.

## Verifying Bug Reports

Evaluate bug reports from the reporter's packet and the matching source. Live LinkedIn reproduction is optional. State which account variant and code version each observation covers. For a chosen local live check, use `uv run` to test the workspace or the reported launcher to test a packaged installation. Ask before login, session changes, or LinkedIn writes.

```bash
# Start server
uv run -m linkedin_mcp_server --transport streamable-http --log-level DEBUG

# Initialize MCP session (grab Mcp-Session-Id from response headers)
curl -s -D /tmp/mcp-headers -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# Extract the session ID from saved headers
SESSION_ID=$(grep -i 'Mcp-Session-Id' /tmp/mcp-headers | awk '{print $2}' | tr -d '\r')

# Call a tool
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_person_profile","arguments":{"linkedin_username":"williamhgates","sections":"posts"}}}'
```

## Live Request Limits

Live checks share one LinkedIn account, so these limits hold across all
sessions and tools together. One tool call can cost several browser actions:
`get_person_profile` loads one page per section, `send_message` takes three,
and `connect_with_person` up to six.

- Per tool: at most 10 calls a minute and 100 a day.
- Profiles: at most one page load a second for `get_person_profile` and
  `get_company_profile`, counted per section.
- Invitations: at most 30 a day, 10 seconds apart. Count every outgoing invitation that
  reports `connected` or `send_failed`, since a failed send may still have
  gone out.
- On a login challenge, a CAPTCHA, or a rate-limit page, stop all live checks
  for 24 hours.

## Release Process

```bash
git checkout main && git pull
uv version --bump minor          # or: major, patch — updates pyproject.toml AND uv.lock
uv run towncrier build --version "$(uv version --short)" --yes
git add pyproject.toml uv.lock
gt create -m "chore: Bump version to X.Y.Z"
gt submit                        # merge PR to trigger release workflow
```

The CI release workflow automatically updates `manifest.json`, `docker-compose.yml` and `server.json` with the new version. Do not update them manually.

After the workflow completes, file a PR against
[`docker/mcp-registry`](https://github.com/docker/mcp-registry) updating
`servers/linkedin-mcp-server/server.yaml`. Docker's own sweep refreshes
`source.commit` only, and only for images in its `mcp/` namespace
(`cmd/ci/update_pins.go`); this entry names `stickerdaniel/linkedin-mcp-server`,
so neither its tag nor its pin ever moves on its own. Skipping it is why that
entry sat on 1.4.0 for a year.

The first such PR is more than a tag: that entry still advertises a
`LINKEDIN_COOKIE` secret and a `USER_AGENT` field for an authentication path
this server no longer has, and `USER_AGENT` now refuses to start
(`config/loaders.py`). It needs the session directory as a mount instead. Docker
validates a changed entry by pulling the image and listing its tools over stdio,
so the tag it moves to has to be a release where that works.

`server.json` is a different registry: the official one at
`registry.modelcontextprotocol.io`, which is a service reached through
`mcp-publisher` and has no PR flow. This server has never been listed there.
Publishing is a maintainer decision rather than a release step, and it cannot
succeed before a release that carries the `mcp-name` token in `README.md` and
the `io.modelcontextprotocol.server.name` label in the `Dockerfile`: ownership
is proved against the *published* PyPI description and image config, so the
markers have to ship first and no local edit can repair an artifact already on
PyPI.

Two things about that entry are outside its own control, both measured against
Docker MCP Gateway, which converts official-registry entries into its catalog.
Neither is a reason to write the entry differently, and both are reasons not to
promise that listing alone makes the server work everywhere.

An `isSecret` variable loses its optionality on the way in, because the catalog
secret keeps only a name and an environment variable, and the add flow then
treats every converted secret as required. The three optional proxy credentials
therefore read as missing for anyone who does not use a proxy, which is nearly
everyone.

A writable host bind needs the operator to name its exact path in
`MCP_GATEWAY_DOCKER_BIND_ALLOW_WRITABLE_PATHS`. By default the gateway allows
binds only under the temporary directories and mounts those read-only, and a
separate variable widens the read-only set without making anything writable. The
session directory has to be written to, and no field in `server.json` can ask
for that.

## Commit Messages

- Follow conventional commits: `type(scope): subject`
- Types: feat, fix, docs, style, refactor, test, chore, perf, ci
- Keep subject <50 chars, imperative mood

## Development Workflow

Always read [`CONTRIBUTING.md`](CONTRIBUTING.md) before filing an issue or working on this repository.

- Write a short synthetic prompt that would reproduce the PR diff if given to a fresh Claude Code session. Don't copy the user's first message — distill the conversation into a single instruction that captures the full scope of changes. This tells the maintainer what was intended, which is often more useful than reviewing the full diff. Use a Markdown blockquote under a `## Synthetic prompt` heading.
- The final non-empty line of every PR body must disclose every model used. CI accepts `Generated with <model>` or `Generated with <model>.` as the minimum. The final period is optional only for this model-only form. Prefer the detailed form `Generated with <model> for <job> in <harness>.`; for example, `Generated with Claude Opus 5 for implementation in Claude Code via T3 Code.` A harness is the coding-agent runtime that invokes the model and tools, such as Claude Code or Codex CLI. Add an outer host or wrapper with optional `via <host>`. For multiple models, use `Generated with <model 1> for <job 1> and <model 2> for <job 2> in <harness>.`; `and` separates model/job pairs exclusively, and commas or `/` list multiple jobs for one model.
- When implementing a new feature/fix:
  1. Packet: before filing or commenting on a GitHub issue, read [.agents/skills/issue-packet/SKILL.md](.agents/skills/issue-packet/SKILL.md).
  2. Branch from `main`: `feature/issue-number-short-description`
  3. Implement and test
  4. Update README.md and docs/docker-hub.md if relevant
  5. Create a draft PR; only convert to regular PR when ready to merge. A `feat`, `fix` or breaking (`type!:`) PR then adds `changelog.d/<PR>.<type>.md`; see CONTRIBUTING
  6. Review with AI agents first, then manual review. PRs are squash-merged into `main` (one commit per PR), so keep the PR title as the conventional-commit subject; commits within a PR are for review only. The squash commit title is `<PR title> (#N)`, which an API or CLI merge sets explicitly.

### Submitting

`gt submit` opens one PR per branch in the stack. To ship a multi-commit change
as a single PR, keep it on one branch and use `gh pr create`.

### Merging a stack

Use `gt merge`, which merges every PR from `main` up to the current branch in
one operation. Do not merge a stack one PR at a time with `gh pr merge`.

The repository deletes the head branch on merge, and that races GitHub's
retargeting of the PR above. Measured twice, both times two seconds apart:
merging the middle PR deleted its branch, and the PR on top of it was closed
before its base could be moved to `main`. A PR closed that way cannot be
reopened or retargeted, so the only way back is to recreate it, which loses its
review history.

If a stacked PR does get closed this way, rebase its branch onto `main`,
force-push with a lease, open a replacement, and leave a comment on the closed
one pointing at it.

## PR Reviews

Greptile reviews no draft on its own, so converting the PR out of draft is
what starts the first pass. A session that opens a draft and then waits for
the bot waits forever.

A later push to a non-draft PR is reviewed again, but Greptile edits its
existing comment instead of posting a new one. A check that looks for a new
comment therefore finds nothing and reads exactly like a pass that never
ran. Verify that its `Last reviewed commit` SHA equals the PR's current head;
a timestamp cannot associate an edit with a pushed revision.

Greptile posts initial reviews as PR review comments, but follow-ups as **issue comments**. Always check both.

```bash
gh api repos/{owner}/{repo}/pulls/{pr}/reviews    # initial reviews
gh api repos/{owner}/{repo}/pulls/{pr}/comments   # inline comments
gh api repos/{owner}/{repo}/issues/{pr}/comments   # follow-up reviews

# A re-review after a push edits the existing comment. Prove which head it saw.
sha='^[0-9a-f]{40}$'
head=$(gh pr view {pr} --json headRefOid --jq .headRefOid) &&
reviewed=$(gh api repos/{owner}/{repo}/issues/{pr}/comments \
  --jq '[.[]
         | select(.user.login == "greptile-apps[bot]")
         | select(.body | contains("Last reviewed commit:"))
         | .body
         | capture("Last reviewed commit:.*?/commit/(?<sha>[0-9a-f]{40})").sha]
        | last') &&
printf '%s\n' "$head" | grep -Eq "$sha" &&
printf '%s\n' "$reviewed" | grep -Eq "$sha" &&
test "$reviewed" = "$head"
```

## btca

When you need up-to-date information about technologies used in this project, use the `btca-local` skill to search the actual source repos. `btca.config.jsonc` is the resource registry; every resource is pre-cloned at `~/.btca/agent/sandbox/<resourceName>` (e.g. `fastmcp`, `playwrightPython`). "Use btca with `<resource>` resource" means: search that clone. If a resource is missing from the sandbox, clone it with the url and branch from the manifest (the skill's "clone main by default" does not apply to registered resources).

**New dependencies:** When adding a new dependency, always add its repo to `btca.config.jsonc` (verify the default branch first: `gh api repos/OWNER/REPO --jq '.default_branch'`) and clone it into the sandbox. Resource names are shared across projects in the sandbox, so pick a name that identifies the repo unambiguously (`playwrightPython`, not `playwright`).
