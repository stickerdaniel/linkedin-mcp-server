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
CREATE TABLE IF NOT EXISTS profile_details (
    slug TEXT PRIMARY KEY,
    profile_text TEXT,
    experience_text TEXT,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS closeness (
    slug TEXT PRIMARY KEY,
    level INTEGER NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_stats (
    slug TEXT PRIMARY KEY,
    messages INTEGER NOT NULL,
    last_message TEXT
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

# Searchable columns, in bm25 weight order after the unindexed slug. The
# profile and experience text come from enrichment; they are long, so they
# weigh least, and a hit there still ranks below the same word in a headline.
_FTS_COLUMNS = ("name", "headline", "company", "position", "profile", "experience")
_FTS_WEIGHTS = "0, 1.0, 3.0, 2.0, 3.0, 0.6, 1.0"
_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE connections_fts USING fts5("
    f"slug UNINDEXED, {', '.join(_FTS_COLUMNS)}, "
    "tokenize = 'unicode61 remove_diacritics 2')"
)

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
        self._ensure_fts()

    def _ensure_fts(self) -> None:
        """Create the search index, or rebuild one an older schema made.

        The index is derived from the tables, so rebuilding it loses nothing;
        a store from before enrichment has no profile or experience column.
        """
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(connections_fts)")]
        if cols == ["slug", *_FTS_COLUMNS]:
            return
        with self._tx() as conn:
            conn.execute("DROP TABLE IF EXISTS connections_fts")
            conn.execute(_FTS_SCHEMA)
            for (slug,) in conn.execute("SELECT slug FROM connections").fetchall():
                self._index(conn, slug)

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
        self._index(conn, values["slug"])
        return not existed

    def _index(self, conn: sqlite3.Connection, slug: str) -> None:
        """Rewrite *slug*'s search row from the tables."""
        row = conn.execute(
            "SELECT c.name, c.first_name, c.last_name, c.headline, c.company, "
            "c.position, d.profile_text, d.experience_text FROM connections c "
            "LEFT JOIN profile_details d ON d.slug = c.slug WHERE c.slug = ?",
            (slug,),
        ).fetchone()
        conn.execute("DELETE FROM connections_fts WHERE slug = ?", (slug,))
        if row is None:
            return
        name = row["name"] or " ".join(
            p for p in (row["first_name"], row["last_name"]) if p
        )
        conn.execute(
            f"INSERT INTO connections_fts (slug, {', '.join(_FTS_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                slug,
                name,
                row["headline"],
                row["company"],
                row["position"],
                row["profile_text"],
                row["experience_text"],
            ),
        )

    def save_details(
        self, slug: str, *, profile_text: str | None, experience_text: str | None
    ) -> None:
        """Store what a profile read returned for one connection."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO profile_details (slug, profile_text, experience_text, "
                "fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
                "profile_text = COALESCE(excluded.profile_text, profile_text), "
                "experience_text = COALESCE(excluded.experience_text, experience_text), "
                "fetched_at = excluded.fetched_at",
                (slug, profile_text, experience_text, _now()),
            )
            self._index(conn, slug)

    def details(self, slug: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT profile_text, experience_text, fetched_at FROM profile_details "
            "WHERE slug = ?",
            (slug,),
        ).fetchone()
        return dict(row) if row else None

    def without_details(self, slugs: Iterable[str]) -> list[str]:
        """The *slugs* that have never been enriched, in the order given."""
        wanted = list(dict.fromkeys(slugs))
        have = (
            {
                r[0]
                for r in self._conn.execute(
                    f"SELECT slug FROM profile_details WHERE slug IN "
                    f"({', '.join('?' for _ in wanted)})",
                    wanted,
                )
            }
            if wanted
            else set()
        )
        return [s for s in wanted if s not in have]

    def get(self, slug: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT *, slug IN (SELECT slug FROM profile_details) AS has_details "
            "FROM connections WHERE slug = ?",
            (slug,),
        ).fetchone()
        return self._public(row) if row else None

    def match_names(self, names: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Stored connections whose name equals each of *names*, case-folded.

        For mutual connections, which LinkedIn names but does not link. A
        name can match several connections; all are returned, and the caller
        says so rather than picking one.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for name in dict.fromkeys(n.strip() for n in names if n and n.strip()):
            rows = self._conn.execute(
                "SELECT * FROM connections WHERE lower(COALESCE(name, "
                "trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')))) "
                "= lower(?)",
                (name,),
            ).fetchall()
            out[name] = [self._public(r) for r in rows]
        return out

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

    def mark(self, slugs: Iterable[str], level: int, note: str | None) -> int:
        """Record how close the member is to each of *slugs* (0-3)."""
        now = _now()
        n = 0
        with self._tx() as conn:
            for slug in slugs:
                conn.execute(
                    "INSERT INTO closeness (slug, level, note, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
                    "level = excluded.level, note = COALESCE(excluded.note, note), "
                    "updated_at = excluded.updated_at",
                    (slug, level, note, now),
                )
                n += 1
        return n

    def replace_message_stats(self, stats: dict[str, tuple[int, str | None]]) -> None:
        """Replace the per-person message counts with a fresh export's."""
        with self._tx() as conn:
            conn.execute("DELETE FROM message_stats")
            conn.executemany(
                "INSERT INTO message_stats (slug, messages, last_message) VALUES (?, ?, ?)",
                [(slug, n, last) for slug, (n, last) in stats.items()],
            )

    def ties(self, slugs: Iterable[str]) -> dict[str, dict[str, Any]]:
        """What the store knows about the member's tie to each of *slugs*."""
        wanted = list(dict.fromkeys(slugs))
        if not wanted:
            return {}
        marks = ", ".join("?" for _ in wanted)
        rows = self._conn.execute(
            "SELECT c.slug, c.connected_on, c.email, k.level, k.note, "
            "m.messages, m.last_message FROM connections c "
            "LEFT JOIN closeness k ON k.slug = c.slug "
            "LEFT JOIN message_stats m ON m.slug = c.slug "
            f"WHERE c.slug IN ({marks})",
            wanted,
        ).fetchall()
        return {r["slug"]: dict(r) for r in rows}

    def all_slugs(self) -> set[str]:
        """Every stored connection's vanity name (export rows without one excluded)."""
        return {
            r[0]
            for r in self._conn.execute(
                "SELECT slug FROM connections WHERE slug NOT LIKE 'csv:%'"
            )
        }

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
            order = f"bm25(connections_fts, {_FTS_WEIGHTS})"
        if company:
            # Past employers too, for connections whose experience is stored.
            where.append(
                "(c.company LIKE ? OR c.headline LIKE ? OR c.slug IN "
                "(SELECT slug FROM profile_details WHERE experience_text LIKE ?))"
            )
            params += [f"%{company}%"] * 3
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
        # The best-matching passage, so a hit in a long experience text says
        # where it matched. Brackets mark the matched words.
        snippet = (
            ", snippet(connections_fts, -1, '[', ']', '…', 16) AS match"
            if fts
            else ", NULL AS match"
        )
        rows = self._conn.execute(
            f"SELECT c.*{snippet}, "
            "c.slug IN (SELECT slug FROM profile_details) AS has_details "
            f"FROM connections c {join} {clause} "
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
            "with_details": q("SELECT COUNT(*) FROM profile_details").fetchone()[0],
            "marked": q("SELECT COUNT(*) FROM closeness").fetchone()[0],
            "with_messages": q("SELECT COUNT(*) FROM message_stats").fetchone()[0],
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
        if "has_details" in row.keys():
            data["has_details"] = bool(row["has_details"])
        data["sources"] = [
            name
            for name, flag in (
                ("page", row["from_page"]),
                ("export", row["from_export"]),
            )
            if flag
        ]
        return data
