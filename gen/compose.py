"""The composer: renders one ambient piece per invocation.

A seeded RNG picks samples from the catalog, then builds four layers over an
intro -> sustain -> outro arc:

  drone   — one long source region, heavily granular-stretched, low-passed,
            stereo-decorrelated, breathing under a slow LFO
  texture — a granular cloud of short grains from several chunks
  motif   — sparse pitch-shifted fragments placed through the sustain
  bed     — an untreated source region, quiet, only low-passed enough to sit back

The dry mix is sent through sox reverb and remixed wet/dry, then written to
render_cache/ with a JSON sidecar carrying full provenance.

Usage:
    python -m gen.compose                # one piece, random seed
    python -m gen.compose --seed deadbeef
    python -m gen.compose --batch 5      # five pieces, distinct seeds
"""

import argparse
import json
import math
import subprocess
import time
import uuid

import numpy as np
import soundfile as sf

from chouse import config, db
from . import dsp

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
TITLE_PATTERNS = [
    "Study for {t}",
    "{t}, dissolved",
    "Variations on {t}",
    "{t} (underwater)",
    "In memoriam: {t}",
    "{t}, slowly",
    "Sea of {t}",
]


def pick_sources(conn, rng):
    """Choose material for one piece, preferring a single dominant item so
    each piece has a coherent identity. Returns (items_used, chunks, drones, beds)."""
    items = [r["item_id"] for r in conn.execute(
        "SELECT DISTINCT item_id FROM samples")]
    if not items:
        raise SystemExit("no samples in catalog — run `python -m prep.run` first")
    hero = items[rng.integers(len(items))]

    def query(kind, item):
        return [r["path"] for r in conn.execute(
            "SELECT path FROM samples WHERE kind = ? AND item_id = ?",
            (kind, item))]

    chunks = query("chunk", hero)
    if not chunks:
        raise SystemExit(f"item {hero} has no chunks")

    items_used = [hero]
    # cross-pollinate with one other item, sometimes
    if len(items) > 1 and rng.random() < 0.35:
        other = items[rng.integers(len(items))]
        if other != hero:
            chunks += query("chunk", other)
            items_used.append(other)

    drones, beds = query("drone", hero), query("bed", hero)
    return items_used, chunks, drones, beds


def build_pad(mix, rng, src, dur_s, cutoff):
    """The centerpiece: a long, chord-like pad.

    2–3 'voices', each a different pitch offset of the source, heavily
    stretched with LONG grains and tiny jitter (short grains + jitter read
    as stuttering echoes; long grains read as a sustained pad), each
    low-passed and level-matched, then all breathing together under one
    very slow LFO.
    """
    voices = int(rng.integers(2, 4))
    layer = np.zeros((len(mix), 2), dtype="float32")
    voice = np.zeros((len(mix), 2), dtype="float32")
    for _ in range(voices):
        voice[:] = 0
        semi = float(rng.choice([-12, -7, -5, 0, 0, 0, 5, 7]))
        vsrc = dsp.pitch_shift(src, semi) if semi else src
        vsrc = dsp.lowpass(vsrc, cutoff * float(rng.uniform(0.8, 1.6)))
        dsp.stretch_into(voice, 0, vsrc, rng,
                         grain_s=float(rng.uniform(5.0, 12.0)),
                         jitter=0.08)
        dsp.rms_normalize(voice, target=0.22)
        layer += voice
    layer *= dsp.lfo(len(layer), float(rng.uniform(0.004, 0.015)),
                     float(rng.uniform(0.3, 0.5)),
                     float(rng.uniform(0, 6.28)))[:, None]
    mix += layer


def build_texture(mix, rng, chunks, start_s, dur_s):
    """Granular grains arriving in 2–4 intermittent 'showers', not one slab."""
    if dur_s <= 5:
        return
    layer = np.zeros((int(dur_s * config.SAMPLE_RATE), 2), dtype="float32")
    for _ in range(int(rng.integers(2, 5))):
        off = float(rng.uniform(0, max(1.0, dur_s - 40)))
        dsp.cloud_into(layer, int(off * config.SAMPLE_RATE), chunks, rng,
                       n_seconds=min(40.0, dur_s - off),
                       grain_s=(float(rng.uniform(0.05, 0.15)),
                                float(rng.uniform(0.3, 0.9))),
                       pitch_range=(-float(rng.uniform(2, 9)),
                                    float(rng.uniform(2, 9))),
                       density=float(rng.uniform(1.5, 4.0)),
                       pan_spread=float(rng.uniform(0.6, 1.0)))
    pos = int(start_s * config.SAMPLE_RATE)
    mix[pos:pos + len(layer)] += layer * 0.35


def build_motifs(mix, rng, chunks, intro_s, outro_s):
    """Sparse pitch-shifted fragments scattered through the sustain.
    Each is level-matched and softened so nothing pokes out of the mix."""
    dur_s = len(mix) / config.SAMPLE_RATE
    for t in sorted(rng.uniform(intro_s, max(intro_s + 1, dur_s - outro_s),
                                int(rng.integers(3, 9)))):
        chunk = chunks[rng.integers(len(chunks))]
        frag_n = int(float(rng.uniform(1.5, 6.0)) * config.SAMPLE_RATE)
        if len(chunk) <= frag_n:
            continue
        start = int(rng.integers(0, len(chunk) - frag_n))
        frag = dsp.pitch_shift(chunk[start:start + frag_n],
                               float(rng.choice([-7, -5, -3, 0, 3, 5, 7])))
        frag = dsp.lowpass(frag, 2500.0)  # tame the sharp top edge
        env = dsp.cosine_fade(len(frag), rising=True) \
            * dsp.cosine_fade(len(frag), rising=False)
        frag = dsp.rms_normalize(frag * env[:, None], target=0.055)
        pan = float(rng.uniform(-0.8, 0.8))
        pos = int(t * config.SAMPLE_RATE)
        end = min(len(mix), pos + len(frag))
        mix[pos:end, 0] += frag[:end - pos, 0] * (0.5 * (1 - pan))
        mix[pos:end, 1] += frag[:end - pos, 1] * (0.5 * (1 + pan))


def build_bed(mix, rng, src, dur_s):
    """The untreated bed, looped quietly underneath everything."""
    src = dsp.lowpass(src, float(rng.uniform(400, 1200)))
    src = dsp.rms_normalize(src, target=0.12)
    gain = float(rng.uniform(0.08, 0.16))
    pos = 0
    while pos < len(mix):
        seg = src[:min(len(src), len(mix) - pos)]
        if len(seg) <= 0:
            break
        mix[pos:pos + len(seg)] += seg * gain
        pos += len(seg)


def smart_title(s: str) -> str:
    """Title-case that leaves apostrophes alone (`.title()` makes Alice'S)."""
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:]
                    for w in s.split())


def make_title(conn, hero, rng):
    row = conn.execute("SELECT title FROM items WHERE identifier = ?",
                       (hero,)).fetchone()
    base = (row["title"] if row and row["title"] else hero)
    base = smart_title(base.split(":")[0].strip())[:40]
    pattern = TITLE_PATTERNS[int(rng.integers(len(TITLE_PATTERNS)))]
    return f"{pattern.format(t=base)} {ROMAN[int(rng.integers(len(ROMAN)))]}"


def compose(seed: str | None = None, duration_s: float | None = None,
            out_dir=None) -> dict:
    """Render one piece; returns its sidecar dict (also written to disk)."""
    seed = seed or uuid.uuid4().hex
    rng = np.random.default_rng(int(seed[:12], 16) if all(
        ch in "0123456789abcdef" for ch in seed[:12]) else abs(hash(seed)))
    duration_s = float(duration_s or rng.uniform(4 * 60, 8 * 60))
    intro_s = float(rng.uniform(45, 90))
    outro_s = float(rng.uniform(60, 120))

    config.ensure_dirs()
    with db.connect() as conn:
        items_used, chunk_paths, drone_paths, bed_paths = pick_sources(conn, rng)

    chunk_data = []
    for p in chunk_paths:
        try:
            seg = dsp.read_segment(p)
            if len(seg):
                chunk_data.append(seg)
        except Exception:
            continue
    if not chunk_data:
        raise SystemExit("no readable chunks")
    drone_src = dsp.read_segment(
        drone_paths[int(rng.integers(len(drone_paths)))]) \
        if drone_paths else chunk_data[0]
    bed_src = dsp.read_segment(bed_paths[int(rng.integers(len(bed_paths)))]) \
        if bed_paths else chunk_data[0]

    n = int(duration_s * config.SAMPLE_RATE)
    mix = np.zeros((n, 2), dtype="float32")
    build_pad(mix, rng, drone_src, duration_s, cutoff=float(rng.uniform(500, 2000)))
    build_texture(mix, rng, chunk_data, intro_s * 0.5, duration_s - intro_s)
    build_motifs(mix, rng, chunk_data, intro_s, outro_s)
    build_bed(mix, rng, bed_src, duration_s)

    dsp.apply_fades(mix, intro_s * 0.8, outro_s * 0.8)
    dsp.loudness_match(mix, target=0.32)

    out_dir = out_dir or config.RENDER_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    dry_path = out_dir / f"{seed}.dry.wav"
    wet_path = out_dir / f"{seed}.wet.wav"
    final_path = out_dir / f"{seed}.wav"
    sf.write(dry_path, mix, config.SAMPLE_RATE, subtype="PCM_16")

    subprocess.run(
        ["sox", str(dry_path), str(wet_path), "reverb",
         "55", "70", "100", "100", "0", "2"],
        check=True, capture_output=True)
    wet, _ = sf.read(wet_path, dtype="float32", always_2d=True)
    final = mix * 0.65 + wet[:len(mix)] * 0.35
    dsp.normalize(final)
    sf.write(final_path, final, config.SAMPLE_RATE, subtype="PCM_16")
    dry_path.unlink(missing_ok=True)
    wet_path.unlink(missing_ok=True)

    with db.connect() as conn:
        title = make_title(conn, items_used[0], rng)
        sidecar = {
            "title": title,
            "seed": seed,
            "duration": duration_s,
            "sources": db.item_sources(conn, items_used),
            "file": str(final_path),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        db.add_piece(conn, final_path, out_dir / f"{seed}.json", title, seed,
                     duration_s, sidecar["sources"])
    sidecar_path = out_dir / f"{seed}.json"
    sidecar_path.write_text(json.dumps(sidecar, indent=2))
    return sidecar


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", help="hex seed (default: random)")
    ap.add_argument("--duration", type=float, help="seconds (default: 4–8 min)")
    ap.add_argument("--batch", type=int, default=1,
                    help="render N pieces with distinct seeds")
    args = ap.parse_args()

    for i in range(args.batch):
        sidecar = compose(seed=args.seed if args.batch == 1 else None,
                          duration_s=args.duration)
        print(f"[{i + 1}/{args.batch}] {sidecar['title']} "
              f"({sidecar['duration'] / 60:.1f} min, seed {sidecar['seed']})")


if __name__ == "__main__":
    main()