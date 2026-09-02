"""DSP building blocks for the composer.

Everything is float32 stereo (n, 2) at the project sample rate. Layer
generators write into a shared mix buffer block-by-block so peak memory
stays small enough for a 1 GB VPS.
"""

import soundfile as sf
import numpy as np

from chouse import config

BLOCK_S = 30  # render granularity, seconds


def read_segment(spec: str) -> np.ndarray:
    """Load a db sample reference like '/path/x.wav#123-4567' -> (n, 2) float32."""
    path, _, span = spec.partition("#")
    start, stop = 0, None
    if span:
        a, b = span.split("-")
        start, stop = int(a), int(b)
    data, sr = sf.read(path, start=start, stop=stop, dtype="float32",
                       always_2d=True)
    if sr != config.SAMPLE_RATE:
        raise ValueError(f"{path}: sample rate {sr} != {config.SAMPLE_RATE}")
    return data


def resample(data: np.ndarray, factor: float) -> np.ndarray:
    """Naive band-limited-ish resample (changes length and pitch together)."""
    if factor == 1.0:
        return data
    n = int(len(data) / factor)
    x = np.linspace(0, len(data) - 1, n)
    out = np.empty((n, data.shape[1]), dtype="float32")
    for c in range(data.shape[1]):
        out[:, c] = np.interp(x, np.arange(len(data)), data[:, c])
    return out


def pitch_shift(data: np.ndarray, semitones: float) -> np.ndarray:
    return resample(data, 2.0 ** (-semitones / 12.0))


def lowpass(data: np.ndarray, cutoff: float) -> np.ndarray:
    """Smooth frequency-domain lowpass (no ringing brickwall edges)."""
    spec = np.fft.rfft(data, axis=0)
    freqs = np.fft.rfftfreq(2 * len(spec) - 1, 1 / config.SAMPLE_RATE)[:len(spec)]
    gain = 1.0 / (1.0 + (freqs / max(cutoff, 1.0)) ** 2)
    return np.fft.irfft(spec * gain[:, None], n=len(data), axis=0).astype("float32")


def hann(n: int) -> np.ndarray:
    return np.hanning(n).astype("float32")


def rms_normalize(data: np.ndarray, target: float = 0.15,
                  out: np.ndarray | None = None) -> np.ndarray:
    """Scale data to a target RMS — keeps individual layers/grains from
    spiking louder than everything else."""
    r = float(np.sqrt((data ** 2).mean()) + 1e-9)
    return np.multiply(data, target / r, out=out if out is not None else None)


def stretch_into(dst: np.ndarray, dst_off: int, src: np.ndarray,
                 rng: np.random.Generator, grain_s: float = 1.2,
                 jitter: float = 0.35, speed: float = 1.0) -> None:
    """Lightweight paulstretch: sequential grains, 50% hann overlap, random
    jitter. Fills dst[dst_off:] (wrapping any excess back to the start so the
    layer is seamless)."""
    grain_n = max(64, int(grain_s * config.SAMPLE_RATE))
    hop = grain_n // 2
    win = hann(grain_n)
    max_start = max(1, len(src) - grain_n)
    src_pos = 0.0
    out_pos = dst_off
    n = len(dst)
    while out_pos < n:
        s = int(np.clip(src_pos + rng.uniform(-jitter, jitter) * grain_n,
                        0, max_start))
        grain = src[s:s + grain_n]
        if len(grain) < grain_n:
            grain = np.pad(grain, ((0, grain_n - len(grain)), (0, 0)))
        grain = (grain * win[:, None]).astype("float32")
        end = out_pos + grain_n
        if end <= n:
            dst[out_pos:end] += grain
        else:  # wrap the tail back to the start of the layer for a clean loop
            wrap = end - n
            dst[out_pos:] += grain[:-wrap]
            dst[dst_off:dst_off + wrap] += grain[-wrap:]
        out_pos += hop
        src_pos += hop * (len(src) / max(1, (n - dst_off))) * speed


def cloud_into(dst: np.ndarray, dst_off: int, chunks: list[np.ndarray],
               rng: np.random.Generator, n_seconds: float,
               grain_s: (float, float) = (0.08, 0.6),
               pitch_range: (float, float) = (-7.0, 7.0),
               density: float = 3.0, pan_spread: float = 1.0) -> None:
    """Granular texture: random grains from the given chunks scattered over
    n_seconds of dst, with random pitch, pan, and window size."""
    n = min(int(n_seconds * config.SAMPLE_RATE), len(dst) - dst_off)
    if n <= 0:
        return
    count = int(n_seconds * density)
    for _ in range(count):
        chunk = chunks[rng.integers(len(chunks))]
        size_s = rng.uniform(*grain_s)  # per-grain window size, seconds
        grain_n = max(64, int(size_s * config.SAMPLE_RATE))
        if len(chunk) <= grain_n:
            continue
        start = int(rng.integers(0, len(chunk) - grain_n))
        grain = pitch_shift(chunk[start:start + grain_n],
                            rng.uniform(*pitch_range))
        grain_n = len(grain)
        win = hann(grain_n)
        pan = rng.uniform(-1, 1) * pan_spread  # -1 left .. +1 right
        gl = grain[:, 0] * win * (0.5 * (1 - pan))
        gr = grain[:, 1] * win * (0.5 * (1 + pan))
        # level-match every grain: sources vary hugely in loudness, and an
        # unnormed grain from a loud passage is exactly the sharp pop we don't want
        g = np.stack([gl, gr], axis=1)
        rms_normalize(g, target=0.10, out=g)
        gl, gr = g[:, 0], g[:, 1]
        out_pos = dst_off + int(rng.integers(0, max(1, n - grain_n)))
        end = out_pos + grain_n
        dst[out_pos:end, 0] += gl[:max(0, min(grain_n, len(dst) - out_pos))]
        dst[out_pos:end, 1] += gr[:max(0, min(grain_n, len(dst) - out_pos))]


def lfo(n: int, freq: float, depth: float, phase: float = 0.0) -> np.ndarray:
    """Slow gain automation, unit-centered."""
    t = np.arange(n) / config.SAMPLE_RATE
    return (1.0 + depth * np.sin(2 * np.pi * freq * t + phase)).astype("float32")


def cosine_fade(n: int, rising: bool) -> np.ndarray:
    x = np.linspace(0, np.pi, n, dtype="float32")
    return (0.5 - 0.5 * np.cos(x)) if rising else (0.5 + 0.5 * np.cos(x))


def apply_fades(buf: np.ndarray, fade_in_s: float, fade_out_s: float) -> None:
    fi = min(int(fade_in_s * config.SAMPLE_RATE), len(buf) // 2)
    fo = min(int(fade_out_s * config.SAMPLE_RATE), len(buf) // 2)
    if fi > 0:
        buf[:fi] *= cosine_fade(fi, rising=True)[:, None]
    if fo > 0:
        buf[-fo:] *= cosine_fade(fo, rising=False)[:, None]


def normalize(buf: np.ndarray, peak_db: float = -1.5) -> np.ndarray:
    peak = np.abs(buf).max()
    if peak > 0:
        buf *= (10 ** (peak_db / 20)) / peak
    return buf


def loudness_match(buf: np.ndarray, target: float = 0.35,
                   percentile: float = 95.0) -> np.ndarray:
    """Scale so the p95 loudness of 50 ms windows sits near `target`, then
    soft-clip. Level-matches pieces for radio consistency and, unlike peak
    normalization, doesn't let one transient drag the whole piece down."""
    w = int(0.05 * config.SAMPLE_RATE)
    frames = len(buf) // w
    if frames < 4:
        return buf
    wrms = np.sqrt((buf[:frames * w].reshape(frames, w, 2) ** 2).mean(axis=(1, 2)))
    voiced = wrms[wrms > 1e-4]
    if not len(voiced):
        return buf
    scale = min(target / float(np.percentile(voiced, percentile)), 6.0)
    np.tanh(buf * scale, out=buf)  # soft ceiling: quiet stays linear, peaks bend
    return buf