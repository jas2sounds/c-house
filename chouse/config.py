"""Shared configuration for C House. All paths are repo-root-relative,
overridable with environment variables so the VPS deployment can relocate them."""

import os
from pathlib import Path

ROOT = Path(os.environ.get("CHOUSE_ROOT", Path(__file__).resolve().parent.parent))

LIBRARY_DIR = Path(os.environ.get("CHOUSE_LIBRARY", ROOT / "library"))
RENDER_DIR = Path(os.environ.get("CHOUSE_RENDER_DIR", ROOT / "render_cache"))
CACHE_DIR = Path(os.environ.get("CHOUSE_CACHE", ROOT / "cache"))
DB_PATH = Path(os.environ.get("CHOUSE_DB", ROOT / "catalog.db"))

SAMPLE_RATE = 44_100
CHANNELS = 2

# archive.org politeness: identify ourselves and stay well under 1 req/s
USER_AGENT = "c-house-generative-radio/0.1 (contact: set CHOUSE_CONTACT_EMAIL)"
CONTACT_EMAIL = os.environ.get("CHOUSE_CONTACT_EMAIL", "")
REQUEST_DELAY = float(os.environ.get("CHOUSE_REQUEST_DELAY", "1.2"))

# search defaults: PD-marked audio only (CC-PD, CC0, and PD-mark all match;
# download.py re-verifies the item-level licenseurl before keeping anything)
PD_LICENSE_QUERY = "licenseurl:*publicdomain*"

# stop ingesting once the sample library reaches this size
LIBRARY_MAX_BYTES = int(float(os.environ.get("CHOUSE_LIBRARY_MAX_GB", "15")) * 1e9)

# keep at most this much disk in rendered pieces; LRU-evicted
RENDER_MAX_BYTES = int(float(os.environ.get("CHOUSE_RENDER_MAX_GB", "2")) * 1e9)


def ensure_dirs() -> None:
    for d in (LIBRARY_DIR, RENDER_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)