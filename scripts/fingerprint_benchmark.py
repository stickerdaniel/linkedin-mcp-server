"""Anti-detection fingerprint benchmark: Patchright vs Camoufox.

Launches each configured browser engine (ephemeral profile -- never touches
the real authenticated LinkedIn profile) against public browser-fingerprint
test sites, saves a screenshot, and captures each site's textual verdict,
then prints a side-by-side comparison. Manual/exploratory tool, not part of
CI or the automated test suite.

Run (NixOS): needs the same native-library resolution as run.sh --
reuse its cache rather than duplicating the ~30-package LIB_PKGS list:
  LD_LIBRARY_PATH="$(tail -n +2 .run-cache/ld_path)" uv run python \\
    scripts/fingerprint_benchmark.py
(run ./run.sh --status once first if .run-cache/ld_path doesn't exist yet)

Run (other platforms): uv run python scripts/fingerprint_benchmark.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).parent.parent))

from linkedin_mcp_server.core.browser import BrowserManager  # noqa: E402

OUTPUT_DIR = Path(__file__).parent / "fingerprint_benchmark_results"


class _Target(TypedDict):
    name: str
    url: str
    wait_seconds: int


_TARGETS: list[_Target] = [
    {"name": "sannysoft", "url": "https://bot.sannysoft.com/", "wait_seconds": 3},
    {
        "name": "creepjs",
        "url": "https://abrahamjuliot.github.io/creepjs/",
        # CreepJS runs its full fingerprint battery client-side; needs a
        # real wait before the verdict text settles.
        "wait_seconds": 8,
    },
    {"name": "pixelscan", "url": "https://pixelscan.net/", "wait_seconds": 5},
]

_ENGINES = ["patchright", "camoufox"]


async def _run_engine(engine: str) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix=f"fingerprint-benchmark-{engine}-") as tmp:
        async with BrowserManager(
            user_data_dir=tmp,
            headless=True,
            engine=engine,
        ) as browser:
            for target in _TARGETS:
                page = browser.page
                try:
                    await page.goto(
                        target["url"], wait_until="domcontentloaded", timeout=30000
                    )
                    await asyncio.sleep(target["wait_seconds"])
                    screenshot_path = OUTPUT_DIR / f"{engine}-{target['name']}.png"
                    await page.screenshot(path=str(screenshot_path))
                    body_text = await page.evaluate(
                        "() => document.body?.innerText || ''"
                    )
                    results[target["name"]] = {
                        "screenshot": str(screenshot_path),
                        "text_excerpt": body_text[:2000],
                    }
                except Exception as exc:
                    results[target["name"]] = {"error": str(exc)}
    return results


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_results: dict[str, dict[str, dict[str, str]]] = {}

    for engine in _ENGINES:
        print(f"\n=== Running {engine} ===")
        all_results[engine] = await _run_engine(engine)
        for target_name, result in all_results[engine].items():
            if "error" in result:
                print(f"  {target_name}: ERROR - {result['error']}")
            else:
                print(f"  {target_name}: screenshot saved to {result['screenshot']}")

    print("\n=== Summary ===")
    for target in _TARGETS:
        name = target["name"]
        print(f"\n--- {name} ---")
        for engine in _ENGINES:
            result = all_results[engine].get(name, {})
            if "error" in result:
                print(f"  {engine}: ERROR - {result['error']}")
            else:
                excerpt = result["text_excerpt"].replace("\n", " ")[:300]
                print(f"  {engine}: {excerpt}")


if __name__ == "__main__":
    asyncio.run(main())
