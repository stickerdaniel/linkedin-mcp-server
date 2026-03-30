import logging

from linkedin_mcp_server.logging_config import configure_logging


def test_configure_logging_sets_up_stderr_handler():
    configure_logging()
    root = logging.getLogger()
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)


def test_configure_logging_json_format():
    configure_logging(json_format=True)
    root = logging.getLogger()
    from linkedin_mcp_server.logging_config import MCPJSONFormatter

    assert any(isinstance(h.formatter, MCPJSONFormatter) for h in root.handlers)
