"""SQLite store for the member's own connections.

One row per connection, keyed by the profile's vanity name. The two sources
fill different columns -- the page gives the headline, the export gives
company, position and email -- so an upsert only ever overwrites a column with
a non-empty value and the row converges on the union of both.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin.my_connections import PageConnection
from linkedin_mcp_server.network.csv_import import CsvConnection

STORE_PATH_ENV = "LINKEDIN_NETWORK_DB"

_COLUMNS = (
    "slug",
    "profile_url",
    "name",
    "first_name",
    "last_name",
    "headline",
    "company",
    "position",
    "email",
    "connected_on",
    "connected_text",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    slug TEXT PRIMARY KEY,
    profile_url TEXT,
    name TEXT,
    first_name TEXT,
    last_name TEXT,
    headline TEXT,
    company TEXT,
    position TEXT,
    email TEXT,
    connected_on TEXT,
    connected_text TEXT,
    from_page INTEGER NOT NULL DEFAULT 0,
    from_export INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS connections_fts USING fts5(
    slug UNINDEXED, name, headline, company, position,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    seen INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
"""

_WORD = re.compile(r"\w+", re.UNICODE)


def default_store_path(profile_dir: Path | str | None = None) -> Path:
    """``$LINKEDIN_NETWORK_DB``, else ``network.db`` beside the browser profile.

    Beside the profile because the store describes the account signed in
    there: a second profile directory is a second account, and gets its own
    store. *profile_dir* must already be canonical (``session_state.
    canonical``): the parent of an unresolved symlink is a different
    directory from the parent of its target, so one profile reached two ways
    would get two stores.
    """
    override = os.environ.get(STORE_PATH_ENV)
    if override:
        return Path(override).expanduser()
    if profile_dir is not None:
        return Path(profile_dir).parent / "network.db"
    return Path.home() / ".linkedin-mcp" / "network.db"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True, slots=True)
class UpsertCounts:
    seen: int
    added: int


def _fts_query(text: str) -> str | None:
    """Turn free text into an FTS5 query: every word, each as a prefix.

    Quoting each token keeps FTS5 syntax (``AND``, ``-``, ``:``) in the
    caller's words from being read as operators.
    """
    words = _WORD.findall(text)
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words)


class NetworkStore:
    """Read and write the local connections database."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_store_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # -- writes ---------------------------------------------------------

    def _upsert(
        self, conn: sqlite3.Connection, values: dict[str, Any], flag: str
    ) -> bool:
        """Merge one row; answer whether it was new."""
        now = _now()
        existed = (
            conn.execute(
                "SELECT 1 FROM connections WHERE slug = ?", (values["slug"],)
            ).fetchone()
            is not None
        )
        cols = [c for c in _COLUMNS if c in values]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{c} = COALESCE(NULLIF(excluded.{c}, ''), connections.{c})"
            for c in cols
            if c != "slug"
        )
        conn.execute(
            f"INSERT INTO connections ({', '.join(cols)}, {flag}, first_seen, updated_at) "
            f"VALUES ({placeholders}, 1, ?, ?) "
            f"ON CONFLICT(slug) DO UPDATE SET {updates}, {flag} = 1, "
            "updated_at = excluded.updated_at",
            [values[c] for c in cols] + [now, now],
        )
        row = conn.execute(
            "SELECT name, first_name, last_name, headline, company, position "
            "FROM connections WHERE slug = ?",
            (values["slug"],),
        ).fetchone()
        name = row["name"] or " ".join(
            p for p in (row["first_name"], row["last_name"]) if p
        )
        conn.execute("DELETE FROM connections_fts WHERE slug = ?", (values["slug"],))
        conn.execute(
            "INSERT INTO connections_fts (slug, name, headline, company, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                values["slug"],
                name,
                row["headline"],
                row["company"],
                row["position"],
            ),
        )
        return not existed

    def upsert_page_connections(
        self, connections: Iterable[PageConnection]
    ) -> UpsertCounts:
        seen = added = 0
        with self._tx() as conn:
            for c in connections:
                seen += 1
                added += self._upsert(
                    conn,
                    {
                        "slug": c.slug,
                        "profile_url": c.profile_url
                        or f"https://www.linkedin.com/in/{c.slug}/",
                        "name": c.name,
                        "headline": c.headline,
                        "connected_text": c.connected_text,
                        "connected_on": c.connected_on,
                    },
                    "from_page",
                )
        return UpsertCounts(seen=seen, added=added)

    def upsert_export_connections(
        self, connections: Iterable[CsvConnection]
    ) -> UpsertCounts:
        seen = added = 0
        with self._tx() as conn:
            for c in connections:
                seen += 1
                full_name = " ".join(p for p in (c.first_name, c.last_name) if p)
                added += self._upsert(
                    conn,
                    {
                        "slug": c.slug,
                        "profile_url": c.profile_url,
                        "name": full_name or None,
                        "first_name": c.first_name,
                        "last_name": c.last_name,
                        "email": c.email,
                        "company": c.company,
                        "position": c.position,
                        "connected_on": c.connected_on,
                    },
                    "from_export",
                )
        return UpsertCounts(seen=seen, added=added)

    def start_run(self, source: str) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO sync_runs (source, started_at) VALUES (?, ?)",
                (source, _now()),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        seen: int,
        added: int,
        complete: bool,
        note: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at = ?, seen = ?, added = ?, "
                "complete = ?, note = ? WHERE id = ?",
                (_now(), seen, added, int(complete), note, run_id),
            )

    # -- reads ----------------------------------------------------------

    def known_slugs(self) -> set[str]:
        return {
            r[0]
            for r in self._conn.execute(
                "SELECT slug FROM connections WHERE from_page = 1"
            )
        }

    def has_complete_walk(self) -> bool:
        """Whether a page walk has ever reached the end of the list.

        Only then does the store hold everything older than its newest
        connection, which is what lets an incremental walk stop at the first
        run of known connections. A walk cut short leaves a gap below what it
        read, and stopping early there would never fill it.
        """
        return (
            self._conn.execute(
                "SELECT 1 FROM sync_runs WHERE source LIKE 'page_%' "
                "AND complete = 1 AND note = 'end_of_list' LIMIT 1"
            ).fetchone()
            is not None
        )

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]

    def search(
        self,
        query: str | None = None,
        *,
        company: str | None = None,
        connected_after: str | None = None,
        connected_before: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Rank by text relevance when *query* is given, else newest first."""
        where: list[str] = []
        params: list[Any] = []
        join = ""
        order = "COALESCE(c.connected_on, c.first_seen) DESC"
        fts = _fts_query(query) if query else None
        if fts:
            join = "JOIN connections_fts f ON f.slug = c.slug"
            where.append("connections_fts MATCH ?")
            params.append(fts)
            # Headline and position carry the role; weight them over the name.
            order = "bm25(connections_fts, 0, 1.0, 3.0, 2.0, 3.0)"
        if company:
            where.append("(c.company LIKE ? OR c.headline LIKE ?)")
            params += [f"%{company}%", f"%{company}%"]
        if connected_after:
            where.append("c.connected_on >= ?")
            params.append(connected_after)
        if connected_before:
            # "2024" and "2024-05" mean through the end of that year or month.
            where.append("c.connected_on <= ?")
            params.append(
                connected_before
                + {4: "-12-31", 7: "-31"}.get(len(connected_before), "")
            )
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM connections c {join} {clause}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"SELECT c.* FROM connections c {join} {clause} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "results": [self._public(r) for r in rows],
        }

    def overview(self, top: int = 15) -> dict[str, Any]:
        q = self._conn.execute
        last_runs = {
            r["source"]: dict(r)
            for r in q(
                "SELECT source, MAX(started_at) AS started_at, finished_at, seen, "
                "added, complete FROM sync_runs GROUP BY source"
            )
        }
        return {
            "store": str(self.path),
            "total": self.count(),
            "from_page": q(
                "SELECT COUNT(*) FROM connections WHERE from_page = 1"
            ).fetchone()[0],
            "from_export": q(
                "SELECT COUNT(*) FROM connections WHERE from_export = 1"
            ).fetchone()[0],
            "with_email": q(
                "SELECT COUNT(*) FROM connections WHERE email IS NOT NULL"
            ).fetchone()[0],
            "top_companies": [
                {"company": r[0], "count": r[1]}
                for r in q(
                    "SELECT company, COUNT(*) n FROM connections "
                    "WHERE company IS NOT NULL GROUP BY company "
                    "ORDER BY n DESC LIMIT ?",
                    (top,),
                )
            ],
            "connections_per_year": [
                {"year": r[0], "count": r[1]}
                for r in q(
                    "SELECT substr(connected_on, 1, 4) y, COUNT(*) FROM connections "
                    "WHERE connected_on IS NOT NULL GROUP BY y ORDER BY y"
                )
            ],
            "last_runs": last_runs,
        }

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        data = {k: row[k] for k in row.keys() if row[k] not in (None, "")}
        for key in ("from_page", "from_export", "first_seen", "updated_at"):
            data.pop(key, None)
        data["sources"] = [
            name
            for name, flag in (
                ("page", row["from_page"]),
                ("export", row["from_export"]),
            )
            if flag
        ]
        return data
