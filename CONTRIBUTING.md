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

## Architecture: One Flag = One Navigation

The scraping engine is built around a **one-flag-one-navigation** design. Understanding this is key to contributing effectively.

### Why This Design?

AI assistants (LLMs) call our MCP tools. Each LinkedIn page navigation takes time and risks rate limits. By mapping each `Flag` to exactly one URL, the LLM can request only the sections it needs — skipping unnecessary navigations while still capturing all available info from each visited page via `innerText` extraction.

### How It Works

**Flag enums** (`scraping/fields.py`) define which pages exist:

```python
class PersonScrapingFields(Flag):
    BASIC_INFO = auto()  # /in/{username}/
    EXPERIENCE = auto()  # /in/{username}/details/experience/
    CONTACT_INFO = auto()  # /in/{username}/overlay/contact-info/
    LANGUAGES = auto()  # /in/{username}/details/languages/
    # ...
```

**Section maps** connect user-facing names to flags:

```python
PERSON_SECTION_MAP = {
    "experience": PersonScrapingFields.EXPERIENCE,
    "contact_info": PersonScrapingFields.CONTACT_INFO,
    # ...
}
```

**Page maps** (`scraping/extractor.py`) wire flags to URLs:

```python
# (flag, section_name, url_suffix, is_overlay)
page_map = [
    (PersonScrapingFields.BASIC_INFO, "main_profile", "/", False),
    (PersonScrapingFields.EXPERIENCE, "experience", "/details/experience/", False),
    (PersonScrapingFields.CONTACT_INFO, "contact_info", "/overlay/contact-info/", True),
    # ...
]
```

The `is_overlay` boolean distinguishes modal overlays (like contact info) from full page navigations — overlays use a different extraction method that reads from the `<dialog>` element.

**Return format** — all scraping tools return:

```python
{"url": str, "sections": {name: raw_text}, "pages_visited": list, "sections_requested": list}
```

## Checklist: Adding a New Section

When adding a section to an existing tool (e.g., adding "certifications" to `get_person_profile`):

### Code

- [ ] Add flag to `PersonScrapingFields` or `CompanyScrapingFields` with URL comment (`scraping/fields.py`)
- [ ] Add entry to `PERSON_SECTION_MAP` or `COMPANY_SECTION_MAP` (`scraping/fields.py`)
- [ ] Add tuple to `page_map` in `scrape_person()` or `scrape_company()` (`scraping/extractor.py`)
- [ ] Update tool docstring with new section name (`tools/person.py` or `tools/company.py`)

### Tests

- [ ] Add flag to `test_atomic_flags_are_distinct` (`tests/test_fields.py`)
- [ ] Add to `test_all_sections` parse test (`tests/test_fields.py`)
- [ ] Update `test_all_flags_visit_all_pages` — add flag, bump count, add to `sections_requested` list, update comment (`tests/test_scraping.py`)
- [ ] Add dedicated navigation test (e.g., `test_certifications_visits_details_page`) (`tests/test_scraping.py`)

### Docs

- [ ] Update tool table in `README.md`
- [ ] Update tool table in `AGENTS.md`
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
- [ ] Update tool table in `AGENTS.md`
- [ ] Update features list in `docs/docker-hub.md`
- [ ] Add tool to `tools` array in `manifest.json`

### Verify

- [ ] `uv run pytest --cov`
- [ ] `uv run ruff check . --fix && uv run ruff format .`
- [ ] `uv run pre-commit run --all-files`

## Workflow

1. [Open an issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) describing the feature or bug
2. Create a branch: `feature/<issue-number>-<short-description>`
3. Implement, test, and update docs (see checklists above)
4. Open a PR — AI agents review first, then manual review
5. Don't squash commits on merge

## Code Style

- **Commits:** conventional commits — `type(scope): subject` (see [CLAUDE.md](CLAUDE.md) for details)
- **Lint/format:** `uv run ruff check . --fix && uv run ruff format .`
- **Type check:** `uv run ty check`
- **Tests:** `uv run pytest --cov`
