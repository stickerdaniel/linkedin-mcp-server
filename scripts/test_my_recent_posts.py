"""Test: fetch and print your most recent LinkedIn posts (logged-in user).

Uses the same code path as the get_my_recent_posts MCP tool.
Requires an existing profile (run `uvx linkedin-scraper-mcp --login` once).

Default profile: ~/.linkedin-mcp/profile
Override with: --user-data-dir /caminho/para/profile

Run:
  python scripts/test_my_recent_posts.py -n 5
  python scripts/test_my_recent_posts.py -n 10 --profile andre-martins-fintech
  python scripts/test_my_recent_posts.py -n 5 --profile https://www.linkedin.com/in/andre-martins-fintech/
  python scripts/test_my_recent_posts.py -n 5 --show   # show browser window
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Parse our script args first; package config will parse argv on first import.
parser = argparse.ArgumentParser(description="Test get_my_recent_posts")
parser.add_argument(
    "--show",
    action="store_true",
    help="Show browser window (default: headless)",
)
parser.add_argument(
    "-n",
    "--limit",
    type=int,
    default=5,
    help="Max number of posts to fetch (default: 5)",
)
parser.add_argument(
    "--profile",
    type=str,
    default=None,
    metavar="USERNAME_OR_URL",
    help="Profile username or URL (e.g. andre-martins-fintech or https://linkedin.com/in/andre-martins-fintech/)",
)
_script_args, _unknown = parser.parse_known_args()
# Leave only script name so linkedin_mcp_server config parser does not see our flags
sys.argv = [sys.argv[0]] + _unknown

sys.path.insert(0, str(Path(__file__).parent.parent))

from linkedin_mcp_server.drivers.browser import (
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
    set_headless,
)
from linkedin_mcp_server.scraping.posts import (
    get_my_recent_posts,
    get_profile_recent_posts,
)


async def main() -> None:
    args = _script_args

    set_headless(not args.show)
    try:
        await ensure_authenticated()
    except Exception as e:
        print(
            "Erro de autenticação. Faça login uma vez:\n  uvx linkedin-scraper-mcp --login\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from e

    browser = await get_or_create_browser()

    if args.profile:
        # Extract username from URL if needed (e.g. .../in/andre-martins-fintech/ -> andre-martins-fintech)
        profile = args.profile.strip()
        if "linkedin.com/in/" in profile:
            profile = profile.split("/in/")[-1].rstrip("/").split("?")[0]
        print(f"Buscando até {args.limit} posts no perfil: {profile}")
        posts = await get_profile_recent_posts(browser.page, profile, limit=args.limit)
    else:
        print(f"Buscando até {args.limit} posts recentes no seu feed...")
        posts = await get_my_recent_posts(browser.page, limit=args.limit)

    await close_browser()

    print(f"\nEncontrados: {len(posts)} post(s)\n")
    if not posts:
        print("Nenhum post encontrado (feed vazio ou seletor DOM diferente).")
        return

    for i, p in enumerate(posts, 1):
        print(f"--- Post {i} ---")
        print("URL:", p.get("post_url", ""))
        print("ID:", p.get("post_id", ""))
        preview = (p.get("text_preview") or "").strip()
        if preview:
            print("Preview:", preview[:300] + ("..." if len(preview) > 300 else ""))
        print()

    print("JSON completo:")
    print(json.dumps(posts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
