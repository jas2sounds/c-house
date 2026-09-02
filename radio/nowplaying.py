"""Public now-playing / provenance page for the station.

Serves a small self-refreshing HTML page: the current piece (the most
recently popped from the queue), what archive.org items it was composed
from, with links and licenses, plus the recent history and how much
generative material is queued ahead.

Usage: python -m radio.nowplaying [--port 8080]
"""

import argparse
import json
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from chouse import config, db

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>@@STATION@@ — now playing</title>
<style>
  body { font: 16px/1.6 -apple-system, sans-serif; max-width: 40rem;
         margin: 4rem auto; padding: 0 1rem; color: #ddd; background: #16161d; }
  h1 { font-weight: 500; letter-spacing: .04em; }
  .track { font-size: 1.5rem; margin: 2rem 0 .5rem; }
  .muted { color: #777; }
  a { color: #9db4ff; }
  li { margin: .6rem 0; }
  hr { border: 0; border-top: 1px solid #2a2a35; margin: 2rem 0; }
</style></head><body>
<h1>@@STATION@@</h1>
<p class="muted">@@BLURB@@</p>
<hr>
<p class="muted">now playing</p>
<p class="track">@@TRACK@@</p>
<p class="muted">composed from:</p>
<ul>@@SOURCES@@</ul>
<hr>
<p class="muted">recently played</p>
<ul>@@RECENT@@</ul>
<p class="muted">@@QUEUED@@ pieces queued (@@HOURS@@ h of listening ahead) ·
every piece is composed live from public-domain audio — it never repeats.</p>
</body></html>"""

BLURB = ("a 24/7 generative ambient radio station. every piece you hear is "
         "rendered on the fly from public-domain recordings on archive.org, "
         "sliced, stretched, and recombined by an algorithmic composer.")


def current_piece():
    with db.connect() as conn:
        row = conn.execute(
            "SELECT path, sidecar, title, played_at FROM pieces "
            "WHERE played_at IS NOT NULL ORDER BY played_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, []
        queued = conn.execute(
            "SELECT COUNT(*) AS n, ROUND(SUM(duration)/3600.0, 1) AS hours "
            "FROM pieces WHERE played_at IS NULL").fetchone()
    sidecar = {}
    if row["sidecar"] and Path(row["sidecar"]).exists():
        try:
            sidecar = json.loads(Path(row["sidecar"]).read_text())
        except json.JSONDecodeError:
            pass
    return sidecar or {"title": row["title"]}, queued


def render():
    station = "C HOUSE"
    sidecar, queued = current_piece()
    if sidecar:
        title = escape(sidecar.get("title", "untitled"))
        sources = "".join(
            f'<li><a href="{escape(s["url"])}">{escape(s["title"])}</a> '
            f'<span class="muted">— {escape(str(s["creator"]))}</span><br>'
            f'<span class="muted">{escape(s["licenseurl"] or "public domain")}</span></li>'
            for s in sidecar.get("sources", []))
    else:
        title, sources = "warming up…", ""
    with db.connect() as conn:
        recent = "".join(
            f'<li>{escape(r["title"] or "")} '
            f'<span class="muted">{r["played_at"]}</span></li>'
            for r in conn.execute(
                "SELECT title, played_at FROM pieces WHERE played_at IS NOT NULL "
                "ORDER BY played_at DESC LIMIT 8 OFFSET 1"))
    out = PAGE
    for token, value in (("@@STATION@@", "C HOUSE"), ("@@BLURB@@", BLURB),
                         ("@@TRACK@@", title), ("@@SOURCES@@", sources),
                         ("@@RECENT@@", recent), ("@@QUEUED@@", str(queued["n"])),
                         ("@@HOURS@@", str(queued["hours"] or 0))):
        out = out.replace(token, value)
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet under systemd
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"serving now-playing on :{args.port}")
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()