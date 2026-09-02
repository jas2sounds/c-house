"""SQLite catalog: ingested items, prepared samples, rendered pieces."""

import json
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  identifier   TEXT PRIMARY KEY,
  title        TEXT,
  creator      TEXT,
  licenseurl   TEXT,
  year         TEXT,
  collection   TEXT,
  bytes        INTEGER DEFAULT 0,
  status       TEXT DEFAULT 'pending',
  downloaded_at TEXT
);
CREATE TABLE IF NOT EXISTS samples (
  id         INTEGER PRIMARY KEY,
  item_id    TEXT REFERENCES items(identifier),
  path       TEXT UNIQUE,
  kind       TEXT,
  duration   REAL,
  rms        REAL,
  centroid   REAL,
  noisiness  REAL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_samples_kind ON samples(kind);
CREATE TABLE IF NOT EXISTS pieces (
  id         INTEGER PRIMARY KEY,
  path       TEXT UNIQUE,
  sidecar    TEXT,
  title      TEXT,
  seed       TEXT UNIQUE,
  duration   REAL,
  sources    TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  played_at  TEXT
);
"""


@contextmanager
def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the queue daemon write while liquidsoap's resolver reads,
    # and busy_timeout absorbs the brief remaining contention windows
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_item(conn, identifier, **fields):
    conn.execute(
        "INSERT INTO items (identifier) VALUES (?) "
        "ON CONFLICT(identifier) DO NOTHING",
        (identifier,),
    )
    for key, value in fields.items():
        conn.execute(f"UPDATE items SET {key} = ? WHERE identifier = ?",
                     (value, identifier))


def add_sample(conn, item_id, path, kind, duration, rms, centroid, noisiness):
    conn.execute(
        "INSERT OR REPLACE INTO samples "
        "(item_id, path, kind, duration, rms, centroid, noisiness) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, str(path), kind, duration, rms, centroid, noisiness),
    )


def add_piece(conn, path, sidecar, title, seed, duration, sources):
    conn.execute(
        "INSERT OR REPLACE INTO pieces "
        "(path, sidecar, title, seed, duration, sources) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(path), str(sidecar), title, seed, duration,
         json.dumps(sources)),
    )


def item_sources(conn, identifiers):
    """Fetch provenance rows for a set of item identifiers."""
    out = []
    for ident in identifiers:
        row = conn.execute(
            "SELECT identifier, title, creator, licenseurl FROM items "
            "WHERE identifier = ?",
            (ident,),
        ).fetchone()
        if row is None:
            continue
        out.append({
            "identifier": row["identifier"],
            "title": row["title"] or row["identifier"],
            "creator": row["creator"] or "unknown",
            "licenseurl": row["licenseurl"] or "",
            "url": f"https://archive.org/details/{row['identifier']}",
        })
    return out