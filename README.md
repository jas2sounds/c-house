# C House

Generative ambient internet radio. Downloads public-domain audio from
archive.org, recombines it into ambient pieces with a DSP pipeline, and
streams the result 24/7 via Icecast + Liquidsoap.

## Layout

- `ingest/` — archive.org search + download + license verification
- `prep/` — normalize, slice, and feature-scan the sample library
- `gen/` — the composer: layered DSP recipes that render pieces
- `radio/` — queue daemon and now-playing/provenance service
- `deploy/` — icecast.xml, radio.liq, systemd units
- `library/` — downloaded samples (gitignored)
- `render_cache/` — generated pieces, LRU-evicted (gitignored)

## Quick start (dev)

```sh
python3 -m venv .venv && .venv/bin/pip install requests numpy soundfile
.venv/bin/python -m ingest.search --max-items 3   # pull a few items
.venv/bin/python -m prep.run                      # normalize + analyze
.venv/bin/python -m gen.compose                   # render one piece
.venv/bin/python -m radio.queue_daemon            # keep the queue stocked
```

Streaming stack lives in `deploy/` — see `deploy/README.md`.