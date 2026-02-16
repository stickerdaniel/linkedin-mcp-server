"""Dump LinkedIn scraper output as timestamped local snapshots.

Uses the same code paths as production (parse_person_sections / parse_company_sections).

Run: uv run python scripts/dump_snapshots.py
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from linkedin_mcp_server.drivers.browser import (
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
    set_headless,
)
from linkedin_mcp_server.scraping import (
    LinkedInExtractor,
    parse_company_sections,
    parse_person_sections,
)

OUTPUT_DIR = Path(__file__).parent / "snapshot_dumps"

# Targets using the same section strings as prod tool calls
PERSON_TARGETS: list[tuple[str, str]] = [
    ("williamhgates", "experience,education,interests,honors,languages,contact_info"),
    ("anistji", "experience,education,honors,languages,contact_info"),
]

COMPANY_TARGETS: list[tuple[str, str]] = [
    ("anthropicresearch", "posts,jobs"),
]


async def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    set_headless(True)

    try:
        await ensure_authenticated()
        browser = await get_or_create_browser()
        extractor = LinkedInExtractor(browser.page)

        for username, sections_str in PERSON_TARGETS:
            print(f"\n--- Scraping person: {username} (sections: {sections_str}) ---")
            fields = parse_person_sections(sections_str)
            result = await extractor.scrape_person(username, fields)

            dump_path = run_dir / f"person_{username}.json"
            dump_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

            for section_name, text in result["sections"].items():
                txt_path = run_dir / f"person_{username}_{section_name}.txt"
                txt_path.write_text(text)
                print(f"  {section_name}: {len(text)} chars")

        for company, sections_str in COMPANY_TARGETS:
            print(f"\n--- Scraping company: {company} (sections: {sections_str}) ---")
            fields = parse_company_sections(sections_str)
            result = await extractor.scrape_company(company, fields)

            dump_path = run_dir / f"company_{company}.json"
            dump_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

            for section_name, text in result["sections"].items():
                txt_path = run_dir / f"company_{company}_{section_name}.txt"
                txt_path.write_text(text)
                print(f"  {section_name}: {len(text)} chars")

    finally:
        await close_browser()

    print(f"\n✅ Snapshots saved to {run_dir}/")


if __name__ == "__main__":
    asyncio.run(main())
