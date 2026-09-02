"""Run the full prep pipeline: normalize every ingested item, then slice
and index it.

Usage:
    python -m prep.run            # process anything new
    python -m prep.run --force    # re-normalize and re-analyze everything
"""

import argparse

from chouse import db
from . import analyze, normalize


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="redo items that were already prepared")
    args = ap.parse_args()

    if args.force:
        with db.connect() as conn:
            conn.execute("DELETE FROM samples")
            for row in conn.execute("SELECT identifier FROM items WHERE status = 'ok'"):
                normalize.prepared_path(row["identifier"]).unlink(missing_ok=True)

    normalize.normalize_all()
    n = analyze.analyze_all()
    print(f"done: {n} segments indexed")


if __name__ == "__main__":
    main()