"""Download audio files for archive.org items into the sample library.

License policy: we only keep an item if the item-level `licenseurl` in its
metadata marks it public domain. The search filter is treated as a hint,
never as proof.
"""

import json
import re
import time
from pathlib import Path

import requests

from chouse import config, db

METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"

AUDIO_SUFFIXES = (".flac", ".wav", ".mp3", ".ogg", ".oga", ".m4a")
# prefer lossless for granular mangling, fall back to mp3
SUFFIX_PRIORITY = {s: i for i, s in enumerate(AUDIO_SUFFIXES)}
MAX_FILE_BYTES = 300_000_000  # skip giant WAVs; plenty of material elsewhere

# covers CC-PD dedication (licenses/publicdomain), CC0 (publicdomain/zero)
# and PD-mark (publicdomain/mark) — anything else is not PD
PD_PATTERNS = (re.compile(r"publicdomain"),)


def is_pd_license(licenseurl: str) -> bool:
    if not licenseurl:
        return False
    return any(p.search(licenseurl) for p in PD_PATTERNS)


def _session() -> requests.Session:
    s = requests.Session()
    ua = config.USER_AGENT
    if config.CONTACT_EMAIL:
        ua += f" <{config.CONTACT_EMAIL}>"
    s.headers["User-Agent"] = ua
    return s


def _throttled_get(session, url, **kwargs):
    time.sleep(config.REQUEST_DELAY)
    return session.get(url, timeout=60, **kwargs)


def _metadata_path(identifier: str) -> Path:
    return config.CACHE_DIR / "meta" / f"{identifier}.json"


def fetch_metadata(session, identifier: str) -> dict:
    """Item metadata, cached on disk to stay polite on re-runs."""
    cache = _metadata_path(identifier)
    if cache.exists():
        return json.loads(cache.read_text())
    resp = _throttled_get(session, METADATA_URL.format(identifier=identifier))
    resp.raise_for_status()
    data = resp.json()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _pick_audio_files(metadata: dict):
    files = []
    for f in metadata.get("files", []):
        name = f.get("name", "")
        if not name.lower().endswith(AUDIO_SUFFIXES):
            continue
        if name.endswith("_sample.mp3"):  # streaming previews, not full files
            continue
        size = int(f.get("size") or 0)
        if size <= 0 or size > MAX_FILE_BYTES:
            continue
        files.append((name, size))
    # one file per item is plenty for a sample library; take the best format
    files.sort(key=lambda fs: SUFFIX_PRIORITY[Path(fs[0]).suffix.lower()])
    return files[:1]


def library_bytes() -> int:
    return sum(p.stat().st_size for p in config.LIBRARY_DIR.rglob("*") if p.is_file())


def library_full() -> bool:
    return library_bytes() >= config.LIBRARY_MAX_BYTES


def download_item(identifier: str) -> str:
    """Download one item. Returns its new status: ok | blocked | failed | skipped."""
    s = _session()
    try:
        metadata = fetch_metadata(s, identifier)
    except requests.RequestException as exc:
        print(f"  ! {identifier}: metadata fetch failed: {exc}")
        return _finish(identifier, "failed")

    m = metadata.get("metadata", {})
    licenseurl = m.get("licenseurl") or ""
    if not is_pd_license(licenseurl):
        print(f"  ! {identifier}: license is not public domain ({licenseurl or 'none'}), skipping")
        return _finish(identifier, "blocked", licenseurl=licenseurl or None)

    if library_full():
        print("  ! library size cap reached, skipping downloads")
        return _finish(identifier, "skipped")

    picked = _pick_audio_files(metadata)
    if not picked:
        print(f"  ! {identifier}: no usable audio files")
        return _finish(identifier, "failed")

    dest_dir = config.LIBRARY_DIR / identifier
    dest_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for name, size in picked:
        dest = dest_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)  # files may live in subpaths
        if dest.exists() and dest.stat().st_size == size:
            total += size
            continue
        url = DOWNLOAD_URL.format(identifier=identifier, filename=name)
        try:
            with _throttled_get(s, url, stream=True) as resp:
                resp.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                tmp.rename(dest)
            total += size
            print(f"  + {identifier}/{name} ({size / 1e6:.1f} MB)")
        except requests.RequestException as exc:
            print(f"  ! {identifier}/{name}: download failed: {exc}")
            dest.unlink(missing_ok=True)
            return _finish(identifier, "failed")

    return _finish(identifier, "ok", bytes=total,
                   title=_scalar(m.get("title")), creator=_scalar(m.get("creator")),
                   licenseurl=licenseurl, year=str(m.get("year") or ""),
                   collection=";".join(_as_list(m.get("collection"))),
                   downloaded_at=time.strftime("%Y-%m-%d %H:%M:%S"))


def _scalar(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _finish(identifier, status, **fields):
    with db.connect() as conn:
        db.upsert_item(conn, identifier, status=status, **fields)
    return status