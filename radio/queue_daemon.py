"""Keep the stream's queue stocked and the render cache bounded.

Runs forever (systemd service). Each cycle:

1. If fewer than AHEAD unplayed pieces exist, render new ones — each is a
   fresh seed, so the stream never repeats. A render costs ~15 s CPU, so
   the queue is usually far ahead of playback (one 6 min piece plays for
   ~25 renders' worth of listening time).
2. Ensure a fallback drone exists for liquidsoap's fallback chain.
3. Evict played pieces' files LRU-style once render_cache exceeds
   RENDER_MAX_BYTES.

Usage: python -m radio.queue_daemon [--once]
"""

import argparse
import os
import time
from pathlib import Path

from chouse import config, db
from gen.compose import compose

AHEAD = int(os.environ.get("CHOUSE_AHEAD", "4"))
INTERVAL_S = 60


def fallback_path() -> Path:
    return config.ROOT / "deploy" / "fallback.wav"


def ensure_fallback() -> None:
    """A ~10 min quiet drone for the (rare) empty-queue case."""
    fb = fallback_path()
    if fb.exists() and fb.stat().st_size > 44:
        return
    print("rendering fallback drone")
    sidecar = compose(seed="fallback-drone-0", duration_s=600)
    rendered = Path(sidecar["file"])
    if rendered.exists():
        fb.parent.mkdir(parents=True, exist_ok=True)
        rendered.replace(fb)


def evict() -> None:
    """Delete played pieces' files (rows stay as provenance) under the cap."""
    files = sorted(config.RENDER_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for path in files:
        if total <= config.RENDER_MAX_BYTES:
            break
        with db.connect() as conn:
            row = conn.execute("SELECT played_at FROM pieces WHERE path = ?",
                               (str(path),)).fetchone()
        if row is None or row["played_at"] is None:
            continue  # never delete something the stream hasn't played yet
        sidecar = path.with_suffix(".json")
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        print(f"evicted {path.name}")


def run_once() -> None:
    ensure_fallback()
    with db.connect() as conn:
        ready = conn.execute(
            "SELECT COUNT(*) AS n FROM pieces WHERE played_at IS NULL").fetchone()["n"]
    if ready < AHEAD:
        print(f"queue low ({ready}/{AHEAD}), rendering")
        sidecar = compose()
        print(f"  + {sidecar['title']} ({sidecar['duration'] / 60:.1f} min)")
    evict()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="run one maintenance pass and exit (for cron/timers)")
    args = ap.parse_args()
    config.ensure_dirs()

    while True:
        try:
            run_once()
        except Exception as exc:  # never die mid-render cycle
            print(f"error: {exc}")
        if args.once:
            return
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()