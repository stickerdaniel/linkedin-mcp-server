"""
Tools over the local store of the member's own network.

None of these touch LinkedIn: they read and write the SQLite file that
sync_my_connections fills from the page and import_connections_csv fills from
LinkedIn's data export. That is what makes them safe to call as often as an
analysis needs.
"""

import logging
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier
from linkedin_mcp_server.session_state import get_source_profile_dir
from linkedin_mcp_server.network.messages_import import parse_messages_csv
from linkedin_mcp_server.network import (
    NetworkStore,
    default_store_path,
    parse_connections_csv,
)

logger = logging.getLogger(__name__)


def open_store() -> NetworkStore:
    """The store for the account in the configured browser profile.

    Read through `get_source_profile_dir`, which expands and resolves the
    configured path together, so a symlinked profile and its target share
    one store.
    """
    return NetworkStore(default_store_path(get_source_profile_dir()))


IsoDate = Annotated[str, Field(pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")]


def register_network_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the offline network-store tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Import Connections CSV",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={"network"},
    )
    async def import_connections_csv(path: str) -> dict[str, Any]:
        """
        Import LinkedIn's Connections.csv export into the local network store.

        The file comes from LinkedIn > Settings > Data privacy > Get a copy of
        your data (tick "Connections"). It adds company, position, email (where
        the connection allows it) and exact connection dates, and merges with
        anything sync_my_connections already read. Safe to re-run with a newer
        export. Does not touch LinkedIn.

        Args:
            path: Path to Connections.csv (a ~ prefix is expanded).

        Returns:
            Dict with rows, added, total_in_store and store path.
        """
        file = Path(path).expanduser()
        if not file.is_file():
            raise CodedToolError(f"No such file: {file}", ErrorCode.INVALID_ARGUMENT)
        try:
            rows = parse_connections_csv(file)
        except ValueError as e:
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e

        store = open_store()
        try:
            run_id = store.start_run("export_csv")
            counts = store.upsert_export_connections(rows)
            store.finish_run(
                run_id, seen=counts.seen, added=counts.added, complete=True
            )
            logger.info("Imported %d connections from %s", counts.seen, file)
            return {
                "rows": counts.seen,
                "added": counts.added,
                "total_in_store": store.count(),
                "store": str(store.path),
            }
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout,
        title="Import Messages CSV",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={"network"},
    )
    async def import_messages_csv(path: str) -> dict[str, Any]:
        """
        Import who you message on LinkedIn, from the messages.csv data export.

        Keeps only a message count and the last message date per person; the
        message text is never stored. find_introductions uses it to prefer
        introducers you actually talk to. Re-running replaces the counts with
        the newer export's. Does not touch LinkedIn.

        Args:
            path: Path to messages.csv from LinkedIn > Settings > Data privacy
                > Get a copy of your data (tick "Messages"). A ~ prefix is
                expanded.

        Returns:
            Dict with people (counterparts counted), in_network (of those, how
            many are stored connections), and store path.
        """
        file = Path(path).expanduser()
        if not file.is_file():
            raise CodedToolError(f"No such file: {file}", ErrorCode.INVALID_ARGUMENT)
        try:
            stats = parse_messages_csv(file)
        except ValueError as e:
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e
        store = open_store()
        try:
            store.replace_message_stats(stats)
            in_network = len(set(stats) & store.all_slugs())
            return {
                "people": len(stats),
                "in_network": in_network,
                "store": str(store.path),
            }
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout,
        title="Mark Connections",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={"network"},
    )
    async def mark_connections(
        usernames: list[str],
        closeness: Annotated[int, Field(ge=0, le=3)],
        note: str | None = None,
    ) -> dict[str, Any]:
        """
        Record how well you know some of your connections.

        find_introductions trusts these marks above every other signal when it
        ranks introducers. Marks are local and can be changed any time by
        marking again. Does not touch LinkedIn.

        Args:
            usernames: LinkedIn usernames or profile URLs, as search_my_network
                returns them in `slug`.
            closeness: 3 close (would happily introduce you), 2 know well,
                1 acquaintance, 0 do not really know them.
            note: Optional reminder, e.g. "PhD co-supervisor".

        Returns:
            Dict with marked (count) and unknown (usernames not in your store).
        """
        slugs = [
            u if u.startswith("csv:") else normalize_person_identifier(u)
            for u in usernames
        ]
        store = open_store()
        try:
            known = [s for s in slugs if store.get(s) is not None]
            marked = store.mark(known, closeness, note)
            return {"marked": marked, "unknown": [s for s in slugs if s not in known]}
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout,
        title="Search My Network",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"network", "search"},
    )
    async def search_my_network(
        query: str | None = None,
        company: str | None = None,
        connected_after: IsoDate | None = None,
        connected_before: IsoDate | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """
        Search your own 1st-degree connections in the local network store.

        Answers from the local file only, so it is instant and can be called as
        many times as an analysis needs. Fill the store first with
        import_connections_csv and/or sync_my_connections.

        To find who to contact for a purpose, run several searches with the
        roles, sectors and organisations that purpose implies (e.g. for a grant
        on AI policy: "policy", "regulation", "ministry", "EU commission",
        "program officer"), then rank the union. Use get_person_profile only on
        the shortlist.

        Args:
            query: Free text matched (word-prefix, any order) against name,
                headline, company and position. Role words rank highest.
                Omit to list connections newest first.
            company: Substring matched against company and headline.
            connected_after: ISO date (YYYY, YYYY-MM or YYYY-MM-DD), inclusive.
            connected_before: ISO date, inclusive.
            limit: Results per page (1-200, default 25).
            offset: Results to skip, for paging.

        Returns:
            Dict with total (matches overall), offset, and results: each with
            slug (LinkedIn username, usable with get_person_profile), name,
            headline, company, position, email, connected_on, profile_url and
            sources (page and/or export).
        """
        store = open_store()
        try:
            return store.search(
                query,
                company=company,
                connected_after=connected_after,
                connected_before=connected_before,
                limit=limit,
                offset=offset,
            )
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Network Person",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"network"},
    )
    async def get_network_person(username: str) -> dict[str, Any]:
        """
        Everything stored about one of your connections, from the local store.

        Includes the profile and experience text that enrich_my_network read,
        when it has, so a fit can be judged from job history and About without
        another LinkedIn visit. Does not touch LinkedIn.

        Args:
            username: The connection's LinkedIn username or profile URL, as
                search_my_network returns it in `slug`.

        Returns:
            The stored connection, plus `details` ({profile_text,
            experience_text, fetched_at}) when it has been enriched.
        """
        slug = (
            username
            if username.startswith("csv:")
            else normalize_person_identifier(username)
        )
        store = open_store()
        try:
            person = store.get(slug)
            if person is None:
                raise CodedToolError(
                    f"{slug} is not in your stored network.", ErrorCode.INVALID_ARGUMENT
                )
            details = store.details(slug)
            if details:
                person["details"] = details
            return person
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Network Overview",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"network"},
    )
    async def get_network_overview() -> dict[str, Any]:
        """
        Summarise the local network store: size, sources, top companies,
        connections per year, and when each source was last synced.

        Call this first to see whether the store is filled and how fresh it
        is. Does not touch LinkedIn.
        """
        store = open_store()
        try:
            return store.overview()
        finally:
            store.close()
