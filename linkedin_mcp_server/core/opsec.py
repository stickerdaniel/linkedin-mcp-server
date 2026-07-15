"""Daily-volume cap, warm-up ramp, and recipient dedup for write-path actions.

Complements ``core/rate_limit.py``'s short-term pacing with a coarser,
persistent (SQLite-backed) daily cap: the single highest-impact protection
against account bans, since no amount of mouse/typing humanization
substitutes for a real limit on daily send/connect volume. Ramps the daily
ceiling up over the first few days of use (a fresh session sending at full
volume from day one is itself a red flag) and refuses to repeat an action
against the same recipient within a configurable window.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    secure_mkdir,
    utcnow_iso,
)

_PRIVATE_FILE_MODE = 0o600
_DEFAULT_DB_PATH = Path.home() / ".linkedin-mcp" / "opsec.db"

# (days-since-first-use, daily-action-ceiling), ascending. The ceiling in
# effect is the cap of the highest threshold already reached; holds at the
# last entry's cap once past it. Tune based on observed usage.
#
# Default schedule -- write actions (connect_with_person, send_message).
WARMUP_SCHEDULE: tuple[tuple[int, int], ...] = (
    (0, 10),
    (3, 20),
    (7, 30),
)

# Higher-ceiling schedule for view_person_profile: viewing a profile is
# materially lower-risk than sending a connection request or message (no
# outbound signal to the target, nothing for LinkedIn to flag as spam),
# so it gets a higher daily ceiling -- but still ramps, and still shares
# the SAME account-wide "days since first use" timeline as the default
# schedule (see _daily_limit): the warm-up concept is about the account
# overall looking new to automation, not about this one action type.
VIEW_PROFILE_WARMUP_SCHEDULE: tuple[tuple[int, int], ...] = (
    (0, 30),
    (3, 60),
    (7, 100),
)

# Action name -> bespoke schedule. Any action not listed here uses
# WARMUP_SCHEDULE (the original default, unchanged for existing callers).
_ACTION_WARMUP_SCHEDULES: dict[str, tuple[tuple[int, int], ...]] = {
    "view_person_profile": VIEW_PROFILE_WARMUP_SCHEDULE,
}

DEFAULT_DEDUP_WINDOW_DAYS = 30


class DailyCapExceededError(Exception):
    """Raised when today's action cap for this action type has been reached."""


class DuplicateRecipientError(Exception):
    """Raised when the same recipient was already targeted within the dedup window."""


@dataclass
class OpsecGate:
    """SQLite-backed daily cap + warm-up ramp + recipient dedup.

    One row per genuinely successful action (see :meth:`record` -- callers
    must only record real successes, mirroring the same "don't count
    inconclusive outcomes" discipline as ``core.rate_limit``).
    """

    db_path: Path = field(default_factory=lambda: _DEFAULT_DB_PATH)
    dedup_window_days: int = DEFAULT_DEDUP_WINDOW_DAYS

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path).expanduser()
        secure_mkdir(self.db_path.parent)
        harden_linkedin_tree(self.db_path.parent)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        if os.name != "nt":
            try:
                self.db_path.chmod(_PRIVATE_FILE_MODE)
            except OSError:
                pass
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_created_at "
                "ON actions(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_recipient "
                "ON actions(action, recipient)"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _parse_iso(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _daily_limit(self, conn: sqlite3.Connection, action: str) -> int:
        schedule = _ACTION_WARMUP_SCHEDULES.get(action, WARMUP_SCHEDULE)
        # "Days since first use" is deliberately account-wide (MIN across
        # ALL actions, not just this one) -- the warm-up concept is about
        # the account overall looking new to automation, not about each
        # action type ramping up independently. Only the ceiling at each
        # stage is action-specific (see `schedule` above).
        row = conn.execute("SELECT MIN(created_at) FROM actions").fetchone()
        first_use = row[0] if row else None
        if not first_use:
            return schedule[0][1]

        days_since = (datetime.now(UTC) - self._parse_iso(first_use)).days
        limit = schedule[0][1]
        for threshold_days, cap in schedule:
            if days_since >= threshold_days:
                limit = cap
        return limit

    def check_daily_cap(self, action: str) -> None:
        """Raise DailyCapExceededError if today's warm-up-adjusted cap for
        *action* has already been reached. Call before attempting the write.
        """
        conn = self._connect()
        try:
            today_prefix = utcnow_iso()[:10]
            today_count = conn.execute(
                "SELECT COUNT(*) FROM actions WHERE action = ? AND created_at LIKE ?",
                (action, f"{today_prefix}%"),
            ).fetchone()[0]
            limit = self._daily_limit(conn, action)
            if today_count >= limit:
                raise DailyCapExceededError(
                    f"Daily cap of {limit} reached for '{action}' today "
                    f"({today_count} already recorded)."
                )
        finally:
            conn.close()

    def check_dedup(self, action: str, recipient: str) -> None:
        """Raise DuplicateRecipientError if *recipient* was already targeted
        by *action* within ``dedup_window_days``.

        Call before attempting the write -- only for actions where
        repeating against the same recipient is itself the risk (e.g.
        sending the same message twice). Skip this for actions like
        connect_with_person, where re-invoking is also how a caller checks
        current connection status and must not be blocked by a past
        success.
        """
        conn = self._connect()
        try:
            cutoff = (
                datetime.now(UTC) - timedelta(days=self.dedup_window_days)
            ).isoformat()
            dup_count = conn.execute(
                "SELECT COUNT(*) FROM actions "
                "WHERE action = ? AND recipient = ? AND created_at >= ?",
                (action, recipient, cutoff),
            ).fetchone()[0]
            if dup_count > 0:
                raise DuplicateRecipientError(
                    f"'{action}' already recorded for recipient '{recipient}' "
                    f"within the last {self.dedup_window_days} days."
                )
        finally:
            conn.close()

    def check(self, action: str, recipient: str) -> None:
        """Raise if *action* against *recipient* should not proceed now.

        Combines :meth:`check_daily_cap` and :meth:`check_dedup`. Does not
        record anything itself -- call :meth:`record` after a genuinely
        successful action.
        """
        self.check_daily_cap(action)
        self.check_dedup(action, recipient)

    def record(self, action: str, recipient: str) -> None:
        """Record a genuinely successful *action* against *recipient*.

        Callers must only call this on real success -- an inconclusive or
        failed outcome must not consume dedup/cap budget.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO actions (action, recipient, created_at) VALUES (?, ?, ?)",
                (action, recipient, utcnow_iso()),
            )
            conn.commit()
        finally:
            conn.close()


_default_gate: OpsecGate | None = None


def get_opsec_gate() -> OpsecGate:
    """Return the process-wide opsec gate singleton."""
    global _default_gate
    if _default_gate is None:
        _default_gate = OpsecGate()
    return _default_gate


def reset_opsec_gate_for_testing() -> None:
    global _default_gate
    _default_gate = None
