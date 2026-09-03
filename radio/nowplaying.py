"""Public now-playing / provenance page for the station.

Serves a self-updating page matching the jas2.com house style (black,
white, OCR-A): the current piece (the most recently popped from the
queue), what archive.org items it was composed from with links and
licenses, recent history, and how much generative material is queued
ahead — plus a play button for the /stream mount. The same data is
available as JSON at /now.json (CORS-enabled, used by jas2.com).

Usage: python -m radio.nowplaying [--port 8080]
"""

import argparse
import json
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from chouse import config, db

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>C HOUSE — now playing</title>
<link rel="stylesheet" href="https://fontlibrary.org/face/ocr-a" type="text/css">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #000; color: #e8e8e8;
         font: 16px/1.75 'OCR-A', 'OCRAExtended', 'Courier New', monospace;
         letter-spacing: .04em; text-transform: lowercase;
         max-width: 38rem; margin: 0 auto; padding: 3rem 1.5rem 4rem; }
  h1 { font-size: 18px; font-weight: normal; letter-spacing: .2em; }
  h1::after { content: ""; display: block; width: 32px; height: 1px;
              background: #666; margin-top: 12px; }
  section { padding: 2rem 0 .5rem; }
  section + section { border-top: 1px solid #1a1a1a; margin-top: 2rem; }
  .player { display: flex; align-items: center; gap: 1.5rem; margin: 2rem 0; }
  #play-btn { width: 76px; height: 76px; border-radius: 50%;
              border: 1px solid #666; background: transparent; color: #e8e8e8;
              font-family: inherit; font-size: 15px; letter-spacing: .12em;
              cursor: pointer; transition: border-color .2s; }
  #play-btn:hover { border-color: #e8e8e8; }
  .status { color: #666; font-size: 14px; letter-spacing: .12em; }
  .status .live { color: #e8e8e8; }
  .meta { color: #666; font-size: 13px; letter-spacing: .15em; margin-bottom: 4px; }
  .track { font-size: 17px; margin-bottom: 4px; }
  ul { list-style: none; }
  li { margin: 10px 0; color: #666; font-size: 14px; }
  a { color: #e8e8e8; text-decoration: none; border-bottom: 1px solid #1a1a1a; }
  a:hover { border-bottom-color: #e8e8e8; }
  .dim { color: #666; }
  a.dim { color: #666; }
  a.dim:hover { color: #e8e8e8; }
</style></head><body>
<h1>c house</h1>

<section>
  <p class="dim">@@BLURB@@</p>
  <div class="player">
    <button id="play-btn" type="button" aria-label="play">&#9654;</button>
    <div class="status" id="player-status">paused</div>
  </div>
</section>

<section>
  <p class="meta">now playing</p>
  <p class="track" id="np-title">@@TRACK@@</p>
  <ul id="np-sources">@@SOURCES@@</ul>
</section>

<section>
  <p class="meta">recently played</p>
  <ul id="np-recent">@@RECENT@@</ul>
  <p class="dim" id="np-queue" style="margin-top: 1rem;">@@QUEUED@@ pieces queued (@@HOURS@@ h of listening ahead) ·
  every piece is composed live from public-domain audio — it never repeats.</p>
</section>

<section>
  <p><a class="dim" href="https://jas2.com">jas2.com</a></p>
</section>

<script>
const btn = document.getElementById("play-btn");
const statusEl = document.getElementById("player-status");
const audio = new Audio();
audio.preload = "none";
let playing = false, connecting = false;

function setStatus(html) { statusEl.innerHTML = html; }

function stop() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  playing = connecting = false;
  btn.textContent = "\\u25B6";
  setStatus("paused");
}

function play() {
  connecting = true;
  setStatus("connecting\\u2026");
  audio.src = "/stream";
  audio.play().then(() => {
    connecting = false; playing = true;
    btn.textContent = "\\u25A0";
    setStatus('<span class="live">live</span>');
  }).catch(() => { stop(); setStatus("couldn't reach the stream — try again"); });
}

btn.addEventListener("click", () => (playing || connecting ? stop() : play()));
audio.addEventListener("stalled", () => { if (playing) setStatus("buffering\\u2026"); });
audio.addEventListener("playing", () => { if (playing) setStatus('<span class="live">live</span>'); });
audio.addEventListener("error", () => {
  if (playing || connecting) { stop(); setStatus("stream dropped — press play to reconnect"); }
});

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function refresh() {
  try {
    const res = await fetch("/now.json", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();
    document.getElementById("np-title").textContent = d.now ? d.now.title : "warming up…";
    document.getElementById("np-sources").innerHTML =
      (d.now && d.now.sources || []).map(s =>
        `<li><a href="${esc(s.url)}">${esc(s.title)}</a> ` +
        `<span class="dim">— ${esc(s.creator || "")}</span><br>` +
        `<span class="dim">${esc(s.licenseurl || "public domain")}</span></li>`).join("");
    document.getElementById("np-recent").innerHTML =
      (d.recent || []).map(r =>
        `<li>${esc(r.title || "")} <span class="dim">${esc(r.played_at || "")}</span></li>`).join("");
    document.getElementById("np-queue").textContent =
      `${d.queued} pieces queued (${d.hours} h of listening ahead) · ` +
      "every piece is composed live from public-domain audio — it never repeats.";
  } catch (e) { /* keep the server-rendered version */ }
}

setInterval(refresh, 30000);
refresh();
</script>
</body></html>"""

BLURB = ("a 24/7 generative ambient radio station. every piece you hear is "
         "rendered on the fly from public-domain recordings on archive.org, "
         "sliced, stretched, and recombined by an algorithmic composer.")

STATION = "C HOUSE"


def snapshot():
    """Current station state as a JSON-serializable dict."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT path, sidecar, title, played_at FROM pieces "
            "WHERE played_at IS NOT NULL ORDER BY played_at DESC LIMIT 1"
        ).fetchone()
        queued = conn.execute(
            "SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(duration),0)/3600.0, 1) AS hours "
            "FROM pieces WHERE played_at IS NULL").fetchone()
        recent = [
            {"title": r["title"], "played_at": r["played_at"]}
            for r in conn.execute(
                "SELECT title, played_at FROM pieces WHERE played_at IS NOT NULL "
                "ORDER BY played_at DESC LIMIT 8 OFFSET 1")
        ]
    now = None
    if row is not None:
        sidecar = {}
        if row["sidecar"] and Path(row["sidecar"]).exists():
            try:
                sidecar = json.loads(Path(row["sidecar"]).read_text())
            except json.JSONDecodeError:
                pass
        now = {
            "title": sidecar.get("title") or row["title"],
            "played_at": row["played_at"],
            "sources": [
                {
                    "title": s.get("title"),
                    "creator": s.get("creator"),
                    "url": s.get("url"),
                    "licenseurl": s.get("licenseurl"),
                }
                for s in sidecar.get("sources", [])
            ],
        }
    return {
        "station": STATION,
        "blurb": BLURB,
        "now": now,
        "recent": recent,
        "queued": queued["n"],
        "hours": queued["hours"] or 0,
    }


def render():
    d = snapshot()
    if d["now"]:
        title = escape(d["now"]["title"] or "untitled")
        sources = "".join(
            f'<li><a href="{escape(s["url"] or "")}">{escape(s["title"] or "")}</a> '
            f'<span class="dim">— {escape(str(s["creator"] or ""))}</span><br>'
            f'<span class="dim">{escape(s["licenseurl"] or "public domain")}</span></li>'
            for s in d["now"]["sources"])
    else:
        title, sources = "warming up…", ""
    recent = "".join(
        f'<li>{escape(r["title"] or "")} '
        f'<span class="dim">{escape(str(r["played_at"] or ""))}</span></li>'
        for r in d["recent"])
    out = PAGE
    for token, value in (("@@BLURB@@", BLURB),
                         ("@@TRACK@@", title), ("@@SOURCES@@", sources),
                         ("@@RECENT@@", recent), ("@@QUEUED@@", str(d["queued"])),
                         ("@@HOURS@@", str(d["hours"]))):
        out = out.replace(token, value)
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = render().encode()
            ctype = "text/html; charset=utf-8"
        elif self.path == "/now.json":
            body = json.dumps(snapshot()).encode()
            ctype = "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
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
