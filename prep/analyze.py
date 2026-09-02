"""Slice prepared WAVs into reusable segments and store acoustic features.

Three segment kinds feed the composer:
  chunk — 2–20 s phrases, cut at energy valleys so they start/end cleanly
  drone — one long (30–60 s) dense region per item, for time-stretching
  bed   — one long (20–40 s) region kept untreated, for texture underneath

Features per segment: RMS, spectral centroid, spectral flatness ("noisiness").
"""

import soundfile as sf
import numpy as np

from chouse import config, db
from .normalize import prepared_path

FRAME = 2048
HOP = 1024
MIN_CHUNK_S = 2.0
MAX_CHUNK_S = 20.0


def frame_rms(path):
    """RMS per FRAME-sample window, block-wise so long files stay cheap."""
    rms = []
    with sf.SoundFile(path) as f:
        while True:
            block = f.read(HOP * 512, dtype="float32", always_2d=True)
            if not len(block):
                break
            mono = block.mean(axis=1)
            n = 1 + max(0, (len(mono) - FRAME)) // HOP
            if n:
                idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
                rms.append(np.sqrt((mono[idx] ** 2).mean(axis=1)))
    return np.concatenate(rms) if rms else np.zeros(1)


def find_valleys(rms, lo, hi):
    """Indices into the frame-rms array roughly `lo`..`hi` seconds apart,
    each at a local energy minimum — clean cut points for segments."""
    sr_frames = config.SAMPLE_RATE / HOP
    min_gap, max_gap = int(lo * sr_frames), int(hi * sr_frames)
    cuts = [0]
    i = min_gap
    while i < len(rms) - min_gap:
        span = rms[i:i + max_gap]
        if not len(span):
            break
        j = i + int(np.argmin(span))
        cuts.append(j)
        i = j + min_gap
    return cuts


def cut_points_to_samples(cuts, rms, path_duration):
    """Convert frame-index cuts to (start, stop) sample ranges, dropping
    silent or over-long leftovers."""
    sr_frames = config.SAMPLE_RATE / HOP
    silence_floor = np.percentile(rms, 25) * 0.35
    ranges = []
    for a, b in zip(cuts, cuts[1:] + [len(rms)]):
        start, stop = int(a * HOP), min(int(b * HOP), int(path_duration * config.SAMPLE_RATE))
        if stop - start < MIN_CHUNK_S * config.SAMPLE_RATE:
            continue
        # pull the ends in to the nearest non-silent frame so segments
        # neither click nor start with dead air
        window = rms[a:b]
        voiced = np.where(window > max(silence_floor, 1e-6))[0]
        if not len(voiced):
            continue
        ranges.append((start + int(voiced[0] * HOP),
                       start + int((voiced[-1] + 1) * HOP)))
    return ranges


def segment_ranges(path):
    """(kind, start, stop) for every usable segment of a prepared WAV."""
    info = sf.info(path)
    duration = info.duration
    rms = frame_rms(path)
    if not np.any(rms > 1e-6):
        return []

    out = []
    # chunks: 2–20 s phrases cut at energy valleys
    for start, stop in cut_points_to_samples(
            [0] + list(find_valleys(rms, 6.0, MAX_CHUNK_S)), rms, duration):
        if (stop - start) / config.SAMPLE_RATE > MAX_CHUNK_S:
            stop = start + int(MAX_CHUNK_S * config.SAMPLE_RATE)
        out.append(("chunk", start, stop))

    # one long dense region for the drone layer
    sr_frames = config.SAMPLE_RATE / HOP
    win = int(45 * sr_frames)
    if len(rms) > win:
        i0 = int(np.argmax(np.convolve(rms, np.ones(win) / win, mode="valid")))
        out.append(("drone", int(i0 * HOP), min(int((i0 + win) * HOP),
                                               int(duration * config.SAMPLE_RATE))))

    # an untreated bed: the longest 30 s valley-cut region, offset from the drone
    bed = sorted(cut_points_to_samples([0] + list(find_valleys(rms, 25.0, 40.0)),
                                       rms, duration),
                 key=lambda r: r[1] - r[0], reverse=True)
    if bed:
        start, stop = bed[0]
        stop = min(stop, start + 40 * config.SAMPLE_RATE)
        # keep the bed disjoint from the drone region when possible
        for kind, dstart, dstop in out:
            if kind == "drone" and not (stop <= dstart or start >= dstop):
                start = dstop
                stop = min(start + 30 * config.SAMPLE_RATE,
                           int(duration * config.SAMPLE_RATE))
                break
        out.append(("bed", start, stop))
    return out


def features(path, start, stop):
    data, _ = sf.read(path, start=start, stop=stop, dtype="float32",
                      always_2d=True)
    mono = data.mean(axis=1)
    rms = float(np.sqrt((mono ** 2).mean()) + 1e-9)

    # spectrum over up to 8 windows spread across the segment
    win = 4096
    if len(mono) < win:
        return rms, 0.0, 1.0
    offs = np.linspace(0, len(mono) - win, min(8, len(mono) // win)).astype(int)
    freqs = np.fft.rfftfreq(win, 1 / config.SAMPLE_RATE)
    centroids, flatness = [], []
    spec_mag_min = 1e-10
    for o in offs:
        spec = np.abs(np.fft.rfft(mono[o:o + win] * np.hanning(win))) ** 2
        centroids.append(float((spec * freqs).sum() / (spec.sum() + 1e-12)))
        log_spec = np.log(spec + spec_mag_min)
        flatness.append(float(np.exp(log_spec.mean()) / (spec.mean() + 1e-12)))
    return rms, float(np.mean(centroids)), float(np.mean(flatness))


def analyze_item(identifier: str) -> int:
    """Slice + index one prepared item. Returns the number of segments."""
    path = prepared_path(identifier)
    if not path.exists():
        return 0
    segments = segment_ranges(path)
    with db.connect() as conn:
        conn.execute("DELETE FROM samples WHERE item_id = ?", (identifier,))
        for kind, start, stop in segments:
            rms, centroid, noisiness = features(path, start, stop)
            seg_path = f"{path}#{start}-{stop}"
            db.add_sample(conn, identifier, seg_path, kind,
                          (stop - start) / config.SAMPLE_RATE,
                          rms, centroid, noisiness)
    return len(segments)


def analyze_all() -> int:
    with db.connect() as conn:
        identifiers = [r["identifier"] for r in conn.execute(
            "SELECT identifier FROM items WHERE status = 'ok'")]
    total = 0
    for i, identifier in enumerate(identifiers):
        n = analyze_item(identifier)
        print(f"[{i + 1}/{len(identifiers)}] {identifier}: {n} segments")
        total += n
    return total