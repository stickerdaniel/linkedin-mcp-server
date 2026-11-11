#!/usr/bin/env python3
"""Entry point for the ``mcp-server-linkedin`` command and ``python -m``."""

import sys

from linkedin_mcp_server import remote_catalog


def main() -> None:
    """Start the optional-tools request, then import and run the server.

    The request goes first and the import second on purpose: importing
    ``cli_main`` is most of the start time, and the request runs alongside it.
    """
    remote_catalog.prefetch(sys.argv[1:])
    from linkedin_mcp_server.cli_main import main as run_server

    run_server()


if __name__ == "__main__":
    main()
