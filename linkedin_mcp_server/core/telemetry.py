"""Lightweight per-scrape telemetry: success/failure, duration, profile
name, buffered in memory and flushed to disk in batches.

Deliberately NOT the more advanced fork's own PerformanceTelemetry design
-- confirmed a real flaw there: a synchronous JSON file write on *every
single record*, no batching, disk I/O on the hot path of every scrape.
This follows this codebase's own established sampling/batching discipline
instead (see core/ip_monitor.py's per-N-calls check, and dependencies.py's
_maybe_check_ip_drift): accumulate in memory, flush in batches -- every
_FLUSH_EVERY_N_RECORDS records, or on an explicit flush() call.

Gated by StealthProfile.telemetry -- callers should skip recording
entirely when it's False, not just skip flushing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TELEMETRY_PATH = Path("~/.linkedin-mcp/telemetry.jsonl").expanduser()
_FLUSH_EVERY_N_RECORDS = 20


@dataclass
class ScrapeRecord:
    timestamp: float
    action: str
    profile_name: str
    duration_seconds: float
    success: bool
    error: str | None = None


class ScrapeTelemetry:
    """In-memory buffer of ScrapeRecords, flushed to a JSONL file in
    batches rather than per-record. One line per record, so the file is
    both streamable and trivially appendable across process restarts.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        flush_every: int = _FLUSH_EVERY_N_RECORDS,
    ) -> None:
        self._path = path or DEFAULT_TELEMETRY_PATH
        self._flush_every = flush_every
        self._buffer: list[ScrapeRecord] = []

    def record(
        self,
        *,
        action: str,
        profile_name: str,
        duration_seconds: float,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Buffer one outcome. Flushes automatically once the buffer
        reaches flush_every records -- never writes to disk on every call.
        """
        self._buffer.append(
            ScrapeRecord(
                timestamp=time.time(),
                action=action,
                profile_name=profile_name,
                duration_seconds=duration_seconds,
                success=success,
                error=error,
            )
        )
        if len(self._buffer) >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        """Append buffered records to disk as JSONL, then clear the
        buffer. Best-effort: a write failure is logged and swallowed --
        telemetry must never break a real scrape."""
        if not self._buffer:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                for record in self._buffer:
                    f.write(json.dumps(asdict(record)) + "\n")
        except Exception:
            logger.debug("Telemetry flush failed (ignored)", exc_info=True)
        finally:
            self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)


_default_telemetry: ScrapeTelemetry | None = None


def get_telemetry() -> ScrapeTelemetry:
    """Return the process-wide telemetry buffer singleton."""
    global _default_telemetry
    if _default_telemetry is None:
        _default_telemetry = ScrapeTelemetry()
    return _default_telemetry


def reset_telemetry_for_testing() -> None:
    global _default_telemetry
    _default_telemetry = None
