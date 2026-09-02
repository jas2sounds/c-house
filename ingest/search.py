"""Find public-domain audio items on archive.org and ingest them.

Searches the advancedsearch API for mediatype:audio items whose licenseurl
marks them public domain, records them in the catalog, then downloads the
best audio file from each (license is re-verified per item during download).

Usage:
    python -m ingest.search --max-items 10
    python -m ingest.search --collection 78rpm_collection --max-items 5
    python -m ingest.search --search-only --max-items 50
"""

import argparse
import random
import time

import requests

from chouse import config, db
from .download import download_item

SEARCH_URL = "https://archive.org/advancedsearch.php"
FIELDS = ["identifier", "title", "creator", "licenseurl", "year", "collection"]


def search_items(query: str, max_items: int, sort: str = "downloads desc",
                 page: int = 1) -> list[dict]:
    params = [("q", query)] + [("fl[]", f) for f in FIELDS] + [
        ("rows", str(min(max_items, 200))),
        ("page", str(page)),
        ("output", "json"),
        ("sort[]", sort),
    ]
    resp = requests.get(SEARCH_URL, params=params, timeout=60,
                        headers={"User-Agent": config.USER_AGENT})
    resp.raise_for_status()
    doc = resp.json().get("response", {})
    return doc.get("docs", [])


def default_query(collection: str | None) -> str:
    parts = ["mediatype:audio", config.PD_LICENSE_QUERY]
    if collection:
        parts.append(f"collection:({collection})")
    return " AND ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", help="raw archive.org query (overrides --collection)")
    ap.add_argument("--collection", help="scope to an archive.org collection")
    ap.add_argument("--max-items", type=int, default=10)
    ap.add_argument("--page", type=int, default=1,
                    help="result page (raise or randomize to explore past the popular items)")
    ap.add_argument("--search-only", action="store_true",
                    help="record matches without downloading")
    ap.add_argument("--download-only", action="store_true",
                    help="skip searching; download pending items already in the catalog")
    args = ap.parse_args()

    config.ensure_dirs()

    if not args.download_only:
        query = args.query or default_query(args.collection)
        print(f"searching: {query}")
        docs = search_items(query, args.max_items, page=args.page)
        with db.connect() as conn:
            for doc in docs:
                collections = doc.get("collection")
                if isinstance(collections, list):
                    collections = ";".join(collections)
                db.upsert_item(conn, doc["identifier"],
                               title=doc.get("title"),
                               creator=doc.get("creator"),
                               licenseurl=doc.get("licenseurl"),
                               year=str(doc.get("year") or ""),
                               collection=collections)
        print(f"recorded {len(docs)} items")

    if args.search_only:
        return

    with db.connect() as conn:
        pending = [r["identifier"] for r in conn.execute(
            "SELECT identifier FROM items WHERE status = 'pending' "
            "ORDER BY identifier")]
    print(f"downloading {len(pending)} pending item(s)")
    for i, identifier in enumerate(pending):
        print(f"[{i + 1}/{len(pending)}] {identifier}")
        status = download_item(identifier)
        print(f"    -> {status}")
        if status == "skipped":  # library cap hit; no point continuing
            break


if __name__ == "__main__":
    main()