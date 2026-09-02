"""Pop the next unplayed piece for liquidsoap.

Liquidsoap's request resolver calls this each time it needs a track. We hand
it the oldest unplayed piece and mark it played at pop time, so the stream
never repeats a piece and the catalog stays consistent without any
liquidsoap-side scripting.

Prints a liquidsoap `annotate:` URI carrying the track title + source credit
so Icecast metadata shows provenance. Prints nothing (exit 0) when the queue
is empty — the fallback chain takes over.

Usage: python -m radio.next_track
"""

import json
from pathlib import Path

from chouse import db


def annotate_safe(text: str) -> str:
    """Strip characters that break liquidsoap's annotate: URI parsing."""
    for ch in ('"', "'", ":", ",", "\n", "\r"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


def next_uri() -> str | None:
    for _ in range(1000):  # bound the skip loop; evictions are rare
        with db.connect() as conn:  # short transaction: read + one update
            row = conn.execute(
                "SELECT id, path, sidecar, title FROM pieces "
                "WHERE played_at IS NULL ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            path = Path(row["path"])
            conn.execute("UPDATE pieces SET played_at = datetime('now') "
                         "WHERE id = ?", (row["id"],))
        if path.exists():
            break
        # evicted or deleted: already marked played, try the next one
    else:
        return None

    sidecar = {}
    if row["sidecar"] and Path(row["sidecar"]).exists():
        try:
            sidecar = json.loads(Path(row["sidecar"]).read_text())
        except json.JSONDecodeError:
            pass
    title = row["title"] or sidecar.get("title") or path.stem
    sources = sidecar.get("sources", [])
    credit = " / ".join(s.get("title", "") for s in sources[:2])
    label = f"{title} — from {credit} (archive.org)" if credit else title
    return f'annotate:title="{annotate_safe(label)}":{path}'


def main():
    uri = next_uri()
    if uri is not None:
        print(uri)


if __name__ == "__main__":
    main()