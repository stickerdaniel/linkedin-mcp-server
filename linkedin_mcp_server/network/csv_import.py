"""Read LinkedIn's ``Connections.csv`` from the "Get a copy of your data" export.

The file opens with a free-text notes preamble before its header row, so the
header is found by its first column rather than assumed to be line one. The
column names are LinkedIn's own and are not translated in the export.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier

_HEADER_FIRST_COLUMN = "First Name"

# "05 Mar 2024" in current exports; older ones wrote "3/5/24".
_DATE_FORMATS = ("%d %b %Y", "%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class CsvConnection:
    """One row of ``Connections.csv``."""

    slug: str
    first_name: str
    last_name: str
    profile_url: str | None
    email: str | None
    company: str | None
    position: str | None
    connected_on: str | None


def _parse_date(value: str) -> str | None:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _slug_from_url(url: str) -> str | None:
    if not url:
        return None
    try:
        return normalize_person_identifier(url)
    except ValueError:
        return None


def _fallback_slug(first: str, last: str) -> str:
    """Key for a row LinkedIn exported without a profile URL.

    Members who opt out of profile sharing come through with the URL blank.
    The prefix keeps these from ever colliding with a real vanity name.
    """
    name = "-".join(part for part in (first, last) if part).lower()
    return f"csv:{name or 'unknown'}"


def parse_connections_csv(source: str | Path) -> list[CsvConnection]:
    """Parse ``Connections.csv`` given as a path or as its text.

    Raises:
        ValueError: When no header row is present, i.e. the file is not a
            LinkedIn connections export.
    """
    if isinstance(source, Path) or (
        "\n" not in source and Path(source).expanduser().is_file()
    ):
        text = Path(source).expanduser().read_text(encoding="utf-8-sig")
    else:
        text = source

    lines = text.splitlines()
    header_at = next(
        (
            i
            for i, line in enumerate(lines)
            if line.lstrip('\ufeff"').startswith(_HEADER_FIRST_COLUMN)
        ),
        None,
    )
    if header_at is None:
        raise ValueError(
            "No 'First Name,Last Name,URL,...' header found: this does not look "
            "like LinkedIn's Connections.csv export"
        )

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_at:])))
    rows: list[CsvConnection] = []
    for record in reader:
        first = (record.get("First Name") or "").strip()
        last = (record.get("Last Name") or "").strip()
        url = (record.get("URL") or "").strip()
        if not (first or last or url):
            continue
        slug = _slug_from_url(url) or _fallback_slug(first, last)
        rows.append(
            CsvConnection(
                slug=slug,
                first_name=first,
                last_name=last,
                profile_url=url or None,
                email=(record.get("Email Address") or "").strip() or None,
                company=(record.get("Company") or "").strip() or None,
                position=(record.get("Position") or "").strip() or None,
                connected_on=_parse_date(record.get("Connected On") or ""),
            )
        )
    return rows
