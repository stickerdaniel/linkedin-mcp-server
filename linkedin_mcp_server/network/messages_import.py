"""Count messages per person from LinkedIn's ``messages.csv`` export.

Only who and when are kept: a count and the last date per counterpart. The
message text never leaves the file. The member's own profile is the one that
takes part in the most messages, which every message includes, so it needs
no configuration.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier


def _slug(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    try:
        return normalize_person_identifier(url)
    except InvalidReferenceError:
        return None


def parse_messages_csv(source: str | Path) -> dict[str, tuple[int, str | None]]:
    """``{vanity name: (messages, last message date)}`` for each counterpart.

    Raises:
        ValueError: When the file has no ``SENDER PROFILE URL`` column, i.e.
            is not LinkedIn's messages export.
    """
    if isinstance(source, Path) or (
        "\n" not in source and Path(source).expanduser().is_file()
    ):
        text = Path(source).expanduser().read_text(encoding="utf-8-sig")
    else:
        text = source
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.strip().upper() for f in reader.fieldnames or []}
    if "SENDER PROFILE URL" not in fields:
        raise ValueError(
            "No 'SENDER PROFILE URL' column: this does not look like LinkedIn's "
            "messages.csv export"
        )

    messages: list[tuple[set[str], str | None]] = []
    appearances: Counter[str] = Counter()
    for record in reader:
        row = {k.strip().upper(): (v or "") for k, v in record.items() if k}
        people = {
            s
            for s in [
                _slug(row.get("SENDER PROFILE URL", "")),
                # Several recipients are separated by commas or spaces.
                *(
                    _slug(u)
                    for u in row.get("RECIPIENT PROFILE URLS", "")
                    .replace(",", " ")
                    .split()
                ),
            ]
            if s
        }
        if not people:
            continue
        date = row.get("DATE", "").strip()[:10] or None
        messages.append((people, date))
        appearances.update(people)

    if not appearances:
        return {}
    me = appearances.most_common(1)[0][0]
    stats: dict[str, tuple[int, str | None]] = {}
    for people, date in messages:
        for other in people - {me}:
            n, last = stats.get(other, (0, None))
            if date and (last is None or date > last):
                last = date
            stats[other] = (n + 1, last)
    return stats
