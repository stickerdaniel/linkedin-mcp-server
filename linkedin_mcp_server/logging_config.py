"""
Logging configuration for LinkedIn MCP Server.

This module provides structured JSON logging for better integration
with MCP clients and monitoring systems.
"""

import json
import logging
from typing import Any, Dict


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


def configure_logging(debug: bool = False, json_format: bool = False) -> None:
    """Configure logging for the LinkedIn MCP Server.

    Args:
        debug: Whether to enable debug logging
        json_format: Whether to use JSON formatting for logs
    """
    log_level = logging.DEBUG if debug else logging.INFO

    if json_format:
        formatter = MCPJSONFormatter()
    else:
        formatter = CompactFormatter()

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Set specific loggers
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
