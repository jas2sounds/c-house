# C House — liquidsoap 2.x configuration
#
# Pulls freshly-rendered generative pieces via `radio.next_track`, crossfades
# them, pushes provenance metadata to Icecast, and falls back to a rendered
# drone if the queue is ever empty.
#
# Note: the source chain is inlined on purpose — liquidsoap's type system
# widens intermediates to fallible, which crossfade() rejects.

root = "/opt/c-house"
python = "#{root}/.venv/bin/python"

def next_track() =
  uri = string.trim(get_process_output("#{python} -m radio.next_track"))
  if uri == "" then
    null
  else
    request.create(uri)
  end
end

generative = request.dynamic(next_track, retry_delay = 5.)
stream = mksafe(crossfade(
  duration = 4.,
  mksafe(fallback([generative, single("#{root}/deploy/fallback.wav")]))))

output.icecast(
  %ffmpeg(format = "mp3", %audio(codec = "libmp3lame", b = "128k")),
  host = "localhost", port = 8000,
  password = "CHANGEME-SOURCE-PASSWORD",
  mount = "stream",
  name = "C House",
  genre = "ambient",
  description = "generative ambient radio, composed live from archive.org public domain audio",
  url = "http://example.org/",
  stream)