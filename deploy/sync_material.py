"""Sync material from this machine to the VPS.

Code travels via git, but the pieces the station actually plays do not live
in the repo: library/, render_cache/, and their catalog.db rows are
gitignored. This script ships the delta:

1. rsync library/ (raw + prepared) — additive, never deletes VPS-side files
2. export catalog rows for items the VPS is missing (+ their samples), with
   local paths rewritten to the VPS layout
3. rsync unplayed pieces' wav + sidecar files, and export their rows
4. apply the SQL on the VPS with INSERT OR IGNORE — play history
   (pieces.played_at) and any VPS-only items are never touched

Usage:
    python -m deploy.sync_material            # full sync
    python -m deploy.sync_material --dry-run  # show what would move

Env overrides: CHOUSE_VPS (host), CHOUSE_VPS_USER, CHOUSE_VPS_ROOT.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from chouse import config, db

VPS_HOST = os.environ.get("CHOUSE_VPS", "170.9.242.84")
VPS_USER = os.environ.get("CHOUSE_VPS_USER", "ubuntu")
VPS_ROOT = Path(os.environ.get("CHOUSE_VPS_ROOT", "/home/ubuntu/c-house"))

SQL_HEADER = "PRAGMA busy_timeout=10000;\nBEGIN;\n"
SQL_FOOTER = "COMMIT;\n"


def vps_target(path: str | None = None) -> str:
    """scp/rsync-style destination: user@host:path."""
    return f"{VPS_USER}@{VPS_HOST}:{path if path is not None else VPS_ROOT}"


def ssh(cmd: str) -> str:
    """Run a (multi-line) shell script on the VPS via stdin."""
    out = subprocess.run(["ssh", f"{VPS_USER}@{VPS_HOST}", "bash"],
                         input=cmd, capture_output=True, text=True, check=True)
    return out.stdout


def vps_items() -> set[str]:
    out = ssh("\n".join([
        f"cd {VPS_ROOT} && .venv/bin/python - <<'PY'",
        "import sqlite3",
        "c = sqlite3.connect('catalog.db')",
        "print('\\n'.join(r[0] for r in c.execute("
        "'SELECT identifier FROM items')))",
        "PY",
    ]))
    return {line.strip() for line in out.splitlines() if line.strip()}


def item_rows(conn, identifiers) -> tuple[str, list[Path]]:
    """SQL for missing items + their samples; also list sample files to ship."""
    sql, files = [], []
    for ident in identifiers:
        item = conn.execute("SELECT * FROM items WHERE identifier = ?",
                            (ident,)).fetchone()
        cols = ",".join(item.keys())
        vals = ",".join("NULL" if item[k] is None else
                        str(item[k]) if isinstance(item[k], (int, float)) else
                        "'" + str(item[k]).replace("'", "''") + "'"
                        for k in item.keys())
        sql.append(f"INSERT OR IGNORE INTO items ({cols}) VALUES ({vals});")
        for s in conn.execute("SELECT item_id, path, kind, duration, rms, "
                              "centroid, noisiness FROM samples "
                              "WHERE item_id = ?", (ident,)):
            # sample paths carry a virtual span suffix: 'x.wav#0-604160'
            local = Path(s["path"].partition("#")[0])
            if not local.exists():
                continue
            files.append(local)
            vpath = str(local).replace(str(config.ROOT), str(VPS_ROOT))
            sql.append(
                "INSERT OR REPLACE INTO samples (item_id, path, kind, "
                "duration, rms, centroid, noisiness) VALUES ("
                f"'{s['item_id']}', '{vpath}', '{s['kind']}', "
                f"{s['duration']}, {s['rms']}, {s['centroid']}, "
                f"{s['noisiness']});")
    return "\n".join(sql), files


def piece_rows(conn) -> tuple[str, list[Path]]:
    """SQL + files for unplayed pieces (sidecar rows ship alongside)."""
    sql, files = [], []
    for p in conn.execute("SELECT path, sidecar, title, seed, duration, "
                          "sources FROM pieces WHERE played_at IS NULL"):
        wav, side = Path(p["path"]), Path(p["sidecar"])
        if not wav.exists():
            continue
        files += [f for f in (wav, side) if f.exists()]
        vwav = str(wav).replace(str(config.ROOT), str(VPS_ROOT))
        vside = str(side).replace(str(config.ROOT), str(VPS_ROOT))
        title = p["title"].replace("'", "''")
        sources = p["sources"].replace("'", "''")
        sql.append(
            "INSERT OR IGNORE INTO pieces (path, sidecar, title, seed, "
            "duration, sources) VALUES ("
            f"'{vwav}', '{vside}', '{title}', '{p['seed']}', "
            f"{p['duration']}, '{sources}');")
    return "\n".join(sql), files


def rsync(paths: list[Path], dest_dir: Path, dry: bool) -> None:
    by_parent: dict[Path, list[str]] = {}
    for p in paths:
        by_parent.setdefault(p.parent, []).append(p.name)
    for parent, names in sorted(by_parent.items()):
        rel_dest = dest_dir / parent.relative_to(config.ROOT)
        print(f"  rsync {parent} -> {rel_dest} ({len(names)} file(s))")
        if dry:
            continue
        subprocess.run(
            ["rsync", "-a", "--relative",
             *[f"./{p.relative_to(config.ROOT)}" for p in paths
               if p.parent == parent],
             vps_target(str(rel_dest))],
            check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"checking catalog on {vps_target()}")
    known = vps_items()
    with db.connect() as conn:
        missing = [r["identifier"] for r in conn.execute(
            "SELECT identifier FROM items WHERE status = 'ok'")
            if r["identifier"] not in known]
        item_sql, sample_files = item_rows(conn, missing)
        piece_sql, piece_files = piece_rows(conn)

    # Never bulk-rsync library/: the VPS may have prepared the shared items
    # from different downloads, and overwriting its prepared/ wavs would
    # invalidate the sample spans already in its catalog. Ship only the new
    # items' raw dirs + their prepared wavs.
    sample_files = list(dict.fromkeys(sample_files))
    raw_dirs = [p for p in (config.LIBRARY_DIR / i for i in missing)
                if p.is_dir()]

    print(f"items to add: {len(missing)} -> {missing}")
    print(f"files to ship: {len(raw_dirs)} raw item dir(s), "
          f"{len(sample_files)} prepared wav(s), "
          f"{len(piece_files)} rendered piece file(s)")

    if not args.dry_run:
        if raw_dirs or sample_files:
            print("syncing new items' library files (targeted, additive)")
            rsync(raw_dirs + sample_files, VPS_ROOT, dry=False)
        if piece_files:
            print("syncing rendered pieces")
            rsync(piece_files, VPS_ROOT, dry=False)

        if item_sql or piece_sql:
            sql = SQL_HEADER + item_sql + "\n" + piece_sql + "\n" + SQL_FOOTER
            with tempfile.NamedTemporaryFile("w", suffix=".sql",
                                             delete=False) as f:
                f.write(sql)
                sql_path = f.name
            subprocess.run(["scp", "-q", sql_path,
                            vps_target("/tmp/c_house_sync.sql")], check=True)
            Path(sql_path).unlink()
            print("applying catalog rows on VPS")
            ssh("\n".join([
                f"cd {VPS_ROOT} && .venv/bin/python - <<'PY'",
                "import sqlite3",
                "c = sqlite3.connect('catalog.db')",
                "c.executescript(open('/tmp/c_house_sync.sql').read())",
                "PY",
            ]))

    print("done — the VPS queue daemon picks up new material on its next cycle")


if __name__ == "__main__":
    sys.exit(main())