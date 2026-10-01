# Contributing

Contributions are welcome. Search the existing issues first, then use the [issue forms](https://github.com/stickerdaniel/linkedin-mcp-server/issues/new/choose) for anything new. AI agents follow the [issue-packet skill](https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/.agents/skills/issue-packet/SKILL.md).

## Setup

```bash
git clone https://github.com/stickerdaniel/linkedin-mcp-server
cd linkedin-mcp-server
uv sync                              # dependencies, including the dev group
uv run pre-commit install
uv run patchright install chromium   # needed for the browser-backed tests
uv run pytest --cov
```

The [README](../README.md#setup-from-source-develop--contribute) covers running the server locally and logging in.

## How the server reads LinkedIn

### Read the rendered page

The server reads what a signed-in LinkedIn page shows. It never calls Voyager or any other private LinkedIn API. [Read the rendered page](../docs/decisions/2026-09-16-rendered-page.md) explains why.

### One section, one navigation

Every section a tool offers maps to exactly one LinkedIn URL, and a section never combines several. The assistant calling the tool asks only for the sections it needs, so it skips the navigations it has no use for, and every page it visits is read in full through `innerText`.

The sections live in `linkedin/fields.py`:

```python
# Maps section name -> (url_suffix, is_overlay)
PERSON_SECTIONS: dict[str, tuple[str, bool]] = {
    "main_profile": ("/", False),
    "experience": ("/details/experience/", False),
    "contact_info": ("/overlay/contact-info/", True),
    # ...
}
```

Overlays such as contact info open as a modal, so they are read from the `<dialog>` element instead of the whole page.

`linkedin/person.py` and `linkedin/company.py` own these sections. `linkedin/extractor.py` is a stable facade that delegates to them. The generated [architecture reference](../docs/linkedin-architecture.md) shows which module owns what, how they import each other, and which ones touch the page directly.

### Minimize DOM dependence

LinkedIn changes its class names, `data-` attributes and component structure often. The server survives that by reading text and navigating by URL:

- Extract data with `innerText`, not `querySelector` or DOM walking.
- Navigate to a URL such as `/details/experience/` instead of clicking through the UI.
- When you do need the DOM, for an `href` that never appears in the text or for a scroll container, use a generic tag and attribute pattern like `a[href*="/jobs/view/"]`. Never use a class name.
- Don't scope a query to a layout container like `.jobs-search-results-list`. It breaks without warning on the next redesign. `main` is the widest scope you should need.
- Leave a comment next to every DOM dependency that explains why text and URLs were not enough.

### Detect state without reading labels

LinkedIn is used in many languages, so never tell connection state, buttons or available actions apart by comparing visible labels such as "Connect", "Message" or "Pending". Rely on URL patterns, on whether an attribute like `aria-label` or `aria-expanded` exists at all, or on structural counts. If text really is the only signal, put it in an explicit per-locale table and say in a comment that other locales are not covered.

### Return format

Every tool that reads LinkedIn returns `{"url": str, "sections": {name: raw_text}}`. `sections` is the main payload. A tool may add:

- `references`: `{section: [{kind, url, text?, context?, value?}]}`, compact links to people, companies and posts. LinkedIn URLs are relative paths to save tokens.
- `section_errors`: `{section: {error_type, error_message, ...}}` for a problem with one section, reported without failing the whole call.
- `unknown_sections`: section names the caller asked for that do not exist.
- `job_ids`: returned by `search_jobs` and `get_saved_jobs`.
- `total` and `promoted_job_ids`: returned by `search_jobs`. `total` is `{count, exact}`, the result count LinkedIn advertises. `promoted_job_ids` is the promoted subset of `job_ids`.

## Adding a section

For example, adding `certifications` to `get_person_profile`.

**Code**

- [ ] Add the entry to `PERSON_SECTIONS` or `COMPANY_SECTIONS` in `linkedin/fields.py`.
- [ ] Add its context and an explicit reference cap in `linkedin/link_metadata.py`.
- [ ] Name the section in the tool docstring in `tools/person.py` or `tools/company.py`.

**Tests**

- [ ] In `tests/test_fields.py`, add it to `test_exported_mapping_retains_exact_tuple_contract_and_identity` and `test_expected_keys`, and for a person section also to `test_all_sections`.
- [ ] In `tests/linkedin/test_person.py` or `tests/linkedin/test_company.py`, add it to the all-sections navigation test and give it its own navigation test, such as `test_certifications_visits_details_page`.

**Docs**

- [ ] Update the tool table in `README.md`, the feature list in `docs/docker-hub.md`, and the tool description in `manifest.json`.

## Adding a tool

For example, `search_companies`.

**Code**

- [ ] Put the workflow in the module that owns it according to `docs/linkedin-architecture.md`. Add a method to `LinkedInExtractor` in `linkedin/extractor.py` only if the facade needs one, and keep it a thin delegate.
- [ ] Add or extend a registration function in `tools/`.
- [ ] If you created a new file there, register it in `create_mcp_server()` in `server.py`.

**Tests**

- [ ] Add a mock method to `_make_mock_extractor` and a test for the tool in `tests/test_tools.py`.
- [ ] Test the workflow in the owner's `tests/linkedin/test_<owner>.py`. If the facade's delegates changed, cover them in `tests/linkedin/test_facade_*.py`.

**Docs**

- [ ] Update the tool table in `README.md` and the feature list in `docs/docker-hub.md`, and add the tool to the `tools` array in `manifest.json`.

## Generated files

Pre-commit fails when either of these is out of date.

### Architecture reference

`docs/linkedin-architecture.md` is generated from the AST of the `linkedin` package, so don't edit it by hand. Regenerate it after you change module imports, public owners, the facade's coroutines or construction state, or which modules touch the page directly:

```bash
uv run python scripts/generate_linkedin_architecture.py
uv run python scripts/generate_linkedin_architecture.py --check
```

### Policy traces

The traces in `tests/fixtures/policy-traces/` record every browser operation the code performs in fixed scenarios against a scripted page, such as navigations, waits and clicks, so a refactor can prove it did not change how the server behaves on LinkedIn. The checker only compares them and refuses to write into that directory. Generate candidates into a directory that does not exist yet and read the whole diff:

```bash
uv run python scripts/check_policy_traces.py --output ../linkedin-mcp-policy-traces
diff -ru tests/fixtures/policy-traces/v1 ../linkedin-mcp-policy-traces
```

Copy them in only if you meant every change the diff shows:

```bash
cp ../linkedin-mcp-policy-traces/*.json tests/fixtures/policy-traces/v1/
rm -rf ../linkedin-mcp-policy-traces
uv run python scripts/check_policy_traces.py --check
```

## Before you open a PR

```bash
uv run pytest --cov
uv run pre-commit run --all-files   # ruff, ty and both generated-file checks
```

## Workflow

1. Link the issue the change belongs to, or open one as described at the top.
2. Branch from `main` as `feature/<issue>-<short-description>` or `fix/<issue>-<short-description>`.
3. Implement the change with tests and docs, following the checklists above.
4. Open the PR as a draft. Title it as a [conventional commit](https://www.conventionalcommits.org/), `type(scope): subject`, with an imperative subject under 50 characters. PRs are squash-merged, so the title becomes the commit subject on `main` and the commits inside the PR are only for review.
5. Finish the attribution line at the end of the PR template. CI fails until it names the model. The preferred form is `Generated with <model> for <job> in <harness>.`, where the job is what the model did, such as implementation or review, and the harness is the coding tool that ran it, such as Claude Code or Codex CLI. Add `via <host>` if another app ran that tool.
6. Add a changelog fragment if the PR needs one (see below).
7. Mark the PR ready for review. AI agents review it first, then a maintainer.

### Changelog fragments

A PR of type `feat` or `fix`, or one marked breaking with a `!` right before the colon (`fix(scope)!: Change the error shape`), needs a changelog fragment. The PR Title check fails until it is there.

1. Add `changelog.d/<PR number>.feat.md`, `.fix.md` or `.breaking.md` to match the title. A breaking title always takes `.breaking.md`.
2. Write one sentence of at most 90 characters that says what changes for users. Leave out the PR link, the release adds it.
3. Push it to the same branch.

If you add or remove the breaking marker later, rename the fragment. Edit an existing fragment instead of running `towncrier create` again, which writes a second, numbered file that the check rejects.
