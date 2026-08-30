"""Approva fork additions.

Everything Approva adds to upstream lives under this package and under
``tools/approva.py``, so a rebase onto ``stickerdaniel/linkedin-mcp-server``
touches new files only. The single edit to upstream code is the pair of lines
in ``server.py`` that registers the tools.
"""
