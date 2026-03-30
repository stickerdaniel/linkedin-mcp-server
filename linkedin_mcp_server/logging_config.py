"""Logging configuration for LinkedIn MCP Server."""

import json
import logging
from typing import Any


class MCPJSONFormatter(logging.Formatter):
    """JSON formatter for MCP stdio mode."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if hasattr(record, "error_type"):
            log_data["error_type"] = record.error_type
        if hasattr(record, "error_details"):
            log_data["error_details"] = record.error_details

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


def configure_logging(log_level: str = "WARNING", json_format: bool = False) -> None:
    """Configure logging with stderr handler."""
    numeric_level = getattr(logging, log_level.upper(), logging.WARNING)

    formatter: logging.Formatter
    if json_format:
        formatter = MCPJSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%H:%M:%S",
        )

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    logging.getLogger("fakeredis").setLevel(logging.WARNING)
    logging.getLogger("docket").setLevel(logging.WARNING)
