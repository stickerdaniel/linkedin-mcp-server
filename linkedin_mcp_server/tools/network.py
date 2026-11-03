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

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.network import (
    NetworkStore,
    default_store_path,
    parse_connections_csv,
)

logger = logging.getLogger(__name__)


def open_store() -> NetworkStore:
    """The store for the account in the configured browser profile."""
    return NetworkStore(default_store_path(get_config().browser.user_data_dir))


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
