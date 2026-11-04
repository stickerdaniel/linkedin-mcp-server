"""A local store of the signed-in member's own 1st-degree network.

Filled from two sources: the connections page read through the browser
(``sync_my_connections``) and LinkedIn's own data export (``Connections.csv``).
Everything here is local and offline: searching the store never touches
LinkedIn, which is the point of keeping it.
"""

from linkedin_mcp_server.network.csv_import import parse_connections_csv
from linkedin_mcp_server.network.store import NetworkStore, default_store_path

__all__ = ["NetworkStore", "default_store_path", "parse_connections_csv"]
