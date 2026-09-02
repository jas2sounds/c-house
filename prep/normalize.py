"""Normalize ingested audio: decode anything to 44.1 kHz stereo WAV,
trim edge silence, loudness-normalize. One prepared WAV per catalog item."""

import subprocess
import sys

from chouse import config, db

FFMPEG = "ffmpeg"
# trim leading/trailing silence below -50 dB peak, then EBU loudnorm to a
# gentle level (headroom matters: the composer stacks several layers)
AF = (
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3:"
    "detection=peak,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3:"
    "detection=peak,"
    "areverse,"
    "loudnorm=I=-20:TP=-2:LRA=11"
)


def prepared_path(identifier: str):
    return config.LIBRARY_DIR / "prepared" / f"{identifier}.wav"


def normalize_item(identifier: str) -> str:
    """Prepare the item's downloaded audio. Returns ok | missing | failed."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM items WHERE identifier = ?", (identifier,)
        ).fetchone()
    if row is None or row["status"] != "ok":
        return "missing"

    dest = prepared_path(identifier)
    if dest.exists() and dest.stat().st_size > 44:
        return "ok"
    dest.parent.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        p for p in (config.LIBRARY_DIR / identifier).rglob("*")
        if p.is_file() and not p.name.endswith(".part")
    ) if (config.LIBRARY_DIR / identifier).exists() else []
    if not sources:
        return "missing"

    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(sources[0]),
        "-af", AF, "-ar", str(config.SAMPLE_RATE),
        "-ac", str(config.CHANNELS), "-c:a", "pcm_s16le",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        print(f"  ! {identifier}: ffmpeg failed: {exc.stderr.strip()[:200]}",
              file=sys.stderr)
        dest.unlink(missing_ok=True)
        return "failed"
    return "ok"


def normalize_all() -> int:
    """Prepare every ingested item. Returns the number prepared."""
    with db.connect() as conn:
        identifiers = [r["identifier"] for r in conn.execute(
            "SELECT identifier FROM items WHERE status = 'ok'")]
    count = 0
    for i, identifier in enumerate(identifiers):
        status = normalize_item(identifier)
        print(f"[{i + 1}/{len(identifiers)}] {identifier} -> {status}")
        if status == "ok":
            count += 1
    return count