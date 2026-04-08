# Contributing

Contributions are welcome! Please [open an issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) first to discuss the feature or bug fix before submitting a PR.

## Development Setup

See the [README](README.md#-local-setup-develop--contribute) for full setup instructions.

```bash
git clone https://github.com/stickerdaniel/linkedin-mcp-server
cd linkedin-mcp-server
uv sync                                    # Install dependencies
uv sync --group dev                        # Install dev dependencies
uv run pre-commit install                  # Set up pre-commit hooks
uv run patchright install chromium         # Install browser
uv run pytest --cov                        # Run tests with coverage
```

## Architecture: One Section = One Navigation

The scraping engine is built around a **one-section-one-navigation** design. Understanding this is key to contributing effectively.

### Why This Design?

AI assistants (LLMs) call our MCP tools. Each LinkedIn page navigation takes time and risks rate limits. By mapping each section to exactly one URL, the LLM can request only the sections it needs — skipping unnecessary navigations while still capturing all available info from each visited page via `innerText` extraction.

### How It Works

**Section config dicts** (`scraping/fields.py`) define which pages exist:

```python
# Maps section name -> (url_suffix, is_overlay)
PERSON_SECTIONS: dict[str, tuple[str, bool]] = {
    "main_profile": ("/", False),
    "experience": ("/details/experience/", False),
    "contact_info": ("/overlay/contact-info/", True),
    "languages": ("/details/languages/", False),
    # ...
}
```

The `is_overlay` boolean distinguishes modal overlays (like contact info) from full page navigations — overlays use a different extraction method that reads from the `<dialog>` element.

The extractor iterates the config dict directly, checking which sections the caller requested:

```python
for section_name, (suffix, is_overlay) in PERSON_SECTIONS.items():
    if section_name not in requested:
        continue
    # navigate and extract...
```

**Return format** — all scraping tools return:

```python
{"url": str, "sections": {name: raw_text}}
# Optional compact link metadata:
{"url": str, "sections": {name: raw_text}, "references": {section: [{kind, url, text?, context?}, ...]}}
# When unknown section names are provided:
{"url": str, "sections": {name: raw_text}, "unknown_sections": [name, ...]}
# search_jobs also returns:
{"url": str, "sections": {name: raw_text}, "job_ids": [id, ...]}
```

`sections` remains the main readable payload. `references` is a compact supplement for entity/article traversal. LinkedIn references are emitted as relative paths to minimize token use.

## Checklist: Adding a New Section

When adding a section to an existing tool (e.g., adding "certifications" to `get_person_profile`):

### Code

- [ ] Add entry to `PERSON_SECTIONS` or `COMPANY_SECTIONS` with `(url_suffix, is_overlay)` (`scraping/fields.py`)
- [ ] Update tool docstring with new section name (`tools/person.py` or `tools/company.py`)

### Tests

- [ ] Add to `test_expected_keys` (`tests/test_fields.py`)
- [ ] Add to `test_all_sections` parse test (`tests/test_fields.py`)
- [ ] Update `test_all_sections_visit_all_urls` — add section to set, update assertions (`tests/test_scraping.py`)
- [ ] Add dedicated navigation test (e.g., `test_certifications_visits_details_page`) (`tests/test_scraping.py`)

### Docs

- [ ] Update tool table in `README.md`
- [ ] Update features list in `docs/docker-hub.md`
- [ ] Update tools array/description in `manifest.json`

### Verify

- [ ] `uv run pytest --cov`
- [ ] `uv run ruff check . --fix && uv run ruff format .`
- [ ] `uv run pre-commit run --all-files`

## Checklist: Adding a New Tool

When adding an entirely new MCP tool (e.g., `search_companies`):

### Code

- [ ] Add extractor method to `LinkedInExtractor` if needed (`scraping/extractor.py`)
- [ ] Add or extend tool registration function (`tools/*.py`)
- [ ] Register tools in `create_mcp_server()` if new file (`server.py`)

### Tests

- [ ] Add mock method to `_make_mock_extractor` (`tests/test_tools.py`)
- [ ] Add tool-level test class/method (`tests/test_tools.py`)
- [ ] Add extractor-level tests if new method (`tests/test_scraping.py`)

### Docs

- [ ] Update tool table in `README.md`
- [ ] Update features list in `docs/docker-hub.md`
- [ ] Add tool to `tools` array in `manifest.json`

### Verify

- [ ] `uv run pytest --cov`
- [ ] `uv run ruff check . --fix && uv run ruff format .`
- [ ] `uv run pre-commit run --all-files`

## Workflow

1. [Open an issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) using the correct GitHub issue template. Fill in every section; delete optional sections if not applicable.
2. Create a branch: `feature/<issue-number>-<short-description>` or `fix/<issue-number>-<short-description>`
3. Implement, test, and update docs (see checklists above)
4. Open a PR — AI agents review first, then manual review
5. Don't squash commits on merge

## Scraping Philosophy: Minimize DOM Dependence

This project favours **innerText extraction and URL navigation** over DOM selectors. LinkedIn's markup changes frequently — class names, `data-` attributes, and component structure are unstable. Our scraping engine is deliberately built to survive those changes:

- **Prefer `innerText`** over `querySelector` / DOM walking for data extraction.
- **Prefer URL navigation** (e.g. `/details/experience/`) over clicking UI elements.
- **When DOM access is unavoidable** (e.g. extracting `href` attributes that don't appear in innerText, finding a scrollable container), keep selectors minimal and generic. Favour tag + attribute patterns (`a[href*="/jobs/view/"]`) over class names (`.jobs-search-results-list`).
- **Never scope queries to layout-specific containers** like `.jobs-search-results-list` — these break silently when LinkedIn redesigns. Use `main` as the broadest acceptable scope.
- **Document any DOM dependency** with a comment explaining why innerText/URL navigation isn't sufficient.

## Code Style

- **Commits:** conventional commits — `type(scope): subject` (see [CLAUDE.md](CLAUDE.md) for details)
- **Lint/format:** `uv run ruff check . --fix && uv run ruff format .`
- **Type check:** `uv run ty check`
- **Tests:** `uv run pytest --cov`
