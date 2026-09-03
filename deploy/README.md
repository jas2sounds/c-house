# Deploying C House

Written for a Debian/Ubuntu VPS. Assumes the repo lands at `/opt/c-house`,
run by a dedicated `c-house` user, with the Python venv at `/opt/c-house/.venv`.

## 1. Provision

```sh
sudo apt update
sudo apt install -y icecast2 liquidsoap ffmpeg sox python3-venv git
sudo useradd -r -m -d /opt/c-house -s /usr/sbin/nologin c-house || true
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile   # if 1 GB RAM
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Install the app

```sh
sudo rsync -a --exclude .venv --exclude library --exclude render_cache \
    ~/c_house/ c-house@vps:/opt/c-house/
# on the VPS:
cd /opt/c-house
python3 -m venv .venv
.venv/bin/pip install requests numpy soundfile
.venv/bin/python -m ingest.search --max-items 40   # build the sample library
.venv/bin/python -m prep.run
.venv/bin/python -m radio.queue_daemon --once      # prime the queue + fallback
```

(The sample library can also be rsynced from the Mac instead of re-ingested:
`rsync -a library/ c-house@vps:/opt/c-house/library/`.)

### Syncing material after ingest (Mac -> VPS)

When new items are ingested and pieces rendered locally, ship the delta with:

```sh
python -m deploy.sync_material            # add --dry-run to preview
```

It rsyncs `library/` and unplayed `render_cache/` pieces, then merges the new
`items`/`samples`/`pieces` rows into the VPS catalog with `INSERT OR IGNORE`
— play history (`pieces.played_at`) and VPS-only items are never touched.
Host/user/target come from `CHOUSE_VPS`, `CHOUSE_VPS_USER`, `CHOUSE_VPS_ROOT`
(defaults: `170.9.242.84`, `c-house`, `/opt/c-house`).

## 3. Secrets

Edit `deploy/icecast.xml` and `deploy/radio.liq`: replace every
`CHANGEME-*-PASSWORD`, and set the contact email in the queue service unit.
Then:

```sh
sudo cp deploy/icecast.xml /etc/icecast2/icecast.xml
sudo chown root:icecast /etc/icecast2/icecast.xml && sudo chmod 640 /etc/icecast2/icecast.xml
sudo systemctl restart icecast2
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now c-house-queue c-house-liquidsoap
```

## 4. Firewall

Open `8000/tcp` to listeners (and SSH). The Icecast admin UI is at
`http://<vps>:8000/admin/` (stats.xsl for the listener list).

## 5. Verify

- `curl -s http://localhost:8000/status.xsl` on the VPS
- from anywhere: open `http://<vps-ip>:8000/stream` in VLC / any browser
- `journalctl -u c-house-queue -f` — watch renders happen as the queue drains
- metadata: Debian's liquidsoap ships the native `%mp3` encoder, which passes
  the `annotate:title` from `radio.next_track` through to Icecast — the
  status page should show "Title — from Source (archive.org)". (Homebrew's
  liquidsoap build lacks native encoders, which is why the config uses
  `%ffmpeg`; on that build ICY titles don't propagate, but the VPS build is fine.)
- leave it 24 h, then check `df -h` (render cache caps itself), RAM, and that
  metadata still shows fresh titles

## Upgrades

`git pull && sudo systemctl restart c-house-queue c-house-liquidsoap`.
The sample library and catalog live outside git; re-run
`python -m ingest.search --max-items 40` any time to widen the pool.