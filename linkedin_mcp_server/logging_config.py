# linkedin_mcp_server/logging_config.py
"""
Logging configuration for LinkedIn MCP Server with format options.

Provides JSON and compact logging formats for different deployment scenarios.
JSON format for production MCP integration, compact format for development.
Includes proper logger hierarchy and external library noise reduction.
"""

import atexit
import json
import logging
from typing import Any, Dict

from linkedin_mcp_server.debug_trace import cleanup_trace_dir, get_trace_dir

_TRACE_FILE_HANDLER: logging.Handler | None = None
_TRACE_CLEANUP_REGISTERED = False


class MCPJSONFormatter(logging.Formatter):
    """JSON formatter for MCP server logs."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON.

        Args:
            record: The log record to format

        Returns:
            JSON-formatted log string
        """
        log_data: Dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add error details if present
        if hasattr(record, "error_type"):
            log_data["error_type"] = record.error_type
        if hasattr(record, "error_details"):
            log_data["error_details"] = record.error_details

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


class CompactFormatter(logging.Formatter):
    """Compact formatter that shortens logger names and uses shorter timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with compact formatting.

        Args:
            record: The log record to format

        Returns:
            Compact-formatted log string
        """
        # Create a copy of the record to avoid modifying the original
        record_copy = logging.LogRecord(
            name=record.name,
            level=record.levelno,
            pathname=record.pathname,
            lineno=record.lineno,
            msg=record.msg,
            args=record.args,
            exc_info=record.exc_info,
            func=record.funcName,
        )
        record_copy.stack_info = record.stack_info

        # Shorten the logger name by removing the linkedin_mcp_server prefix
        if record_copy.name.startswith("linkedin_mcp_server."):
            record_copy.name = record_copy.name[len("linkedin_mcp_server.") :]

        # Format the time as HH:MM:SS only
        record_copy.asctime = self.formatTime(record_copy, datefmt="%H:%M:%S")

        return f"{record_copy.asctime} - {record_copy.name} - {record.levelname} - {record.getMessage()}"


def configure_logging(log_level: str = "WARNING", json_format: bool = False) -> None:
    """Configure logging for the LinkedIn MCP Server.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        json_format: Whether to use JSON formatting for logs
    """
    # Convert string to logging level
    numeric_level = getattr(logging, log_level.upper(), logging.WARNING)

    if json_format:
        formatter = MCPJSONFormatter()
    else:
        formatter = CompactFormatter()

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    global _TRACE_CLEANUP_REGISTERED, _TRACE_FILE_HANDLER
    _TRACE_FILE_HANDLER = None

    # Add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    trace_dir = get_trace_dir()
    if trace_dir is not None:
        trace_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(trace_dir / "server.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        _TRACE_FILE_HANDLER = file_handler
        if not _TRACE_CLEANUP_REGISTERED:
            # The atexit fallback intentionally delegates the keep/delete
            # decision to teardown_trace_logging(), which re-checks runtime
            # trace retention state via cleanup_trace_dir().
            atexit.register(teardown_trace_logging)
            _TRACE_CLEANUP_REGISTERED = True

    # Set specific loggers to reduce noise
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    logging.getLogger("fakeredis").setLevel(logging.WARNING)
    logging.getLogger("docket").setLevel(logging.WARNING)


def teardown_trace_logging(*, keep_traces: bool = False) -> None:
    """Close trace logging handlers and cleanup ephemeral traces when allowed."""
    global _TRACE_FILE_HANDLER

    if _TRACE_FILE_HANDLER is not None:
        root_logger = logging.getLogger()
        root_logger.removeHandler(_TRACE_FILE_HANDLER)
        try:
            _TRACE_FILE_HANDLER.close()
        finally:
            _TRACE_FILE_HANDLER = None

    if not keep_traces:
        cleanup_trace_dir()
