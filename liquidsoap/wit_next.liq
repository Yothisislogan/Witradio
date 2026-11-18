# WIT-FM playout master (development plan, Phase 1; show bible, Lossless audio spec)
#
# One 16-bit/44.1 kHz master feeds every output, encoded in parallel:
#   /lossless   Ogg FLAC, 24/7: the website player, VLC, hi-fi streamers
#   /next       MP3 320 kbps (becomes /stream at the cutover). The YouTube
#               relay (mac/youtube_relay.sh) reads this mount.
#
# mac/playout_director.py decides every item; Liquidsoap asks it over
# localhost HTTP and never spawns processes (doing that from its threads
# crashed Liquidsoap 2.2.4 in rehearsal). Classical works carry one fixed
# gain each and no fades, crossfades, compression or limiting; works are
# separated by the planner's 2-4 s of silence. If the director fails, the chain
# falls back to the safe library, then station IDs, then silence, and an SMS
# goes out after 20 s of dead air.
#
# Start it with mac/playout.sh (or `./writ --station wit-next start playout`),
# which exports the station's settings. Needs Liquidsoap 2.2 or newer.

def env(name, default="") =
  environment.get(default=default, name)
end

if env("WRIT_PLAYOUT_ALLOW_ROOT") == "1" then
  settings.init.allow_root := true
end
settings.log.stdout := true
settings.log.file := false
settings.log.level := int_of_string(env("WRIT_PLAYOUT_LOG_LEVEL", "3"))
settings.frame.audio.samplerate := 44100
settings.frame.audio.channels := 2

# Decode FLAC with libflac: its samples round-trip bit for bit; the FFmpeg
# decoder's do not. The Phase 1 gate (a test FLAC at unity gain comes out of
# /lossless bit for bit) depends on this. One exception, inside Liquidsoap:
# samples travel as floats scaled by 32767 and clipped to [-1, 1], so the
# single value -32768 airs as -32767 (1 LSB at negative full scale). Every
# other 16-bit value is exact (tests/test_playout_liquidsoap.py).
settings.decoder.priorities.flac := 100


library = env("WRIT_LIBRARY_DIR")
director = "http://127.0.0.1:#{env('WRIT_DIRECTOR_PORT', '8102')}"

# WRIT_PLAYOUT_TO_FILES=<dir> writes both outputs to files instead of Icecast
# (for tests and the bit-for-bit check).
to_files = env("WRIT_PLAYOUT_TO_FILES")
password_var = env("WRIT_SOURCE_PASSWORD_ENV", "WRIT_SOURCE_PASSWORD")
password = env(password_var)
if password == "" and to_files == "" then
  log.critical(label="playout", "#{password_var} is not set: export the Icecast source password first")
  shutdown(code=2)
end
host = env("WRIT_ICECAST_HOST", "localhost")
port = int_of_string(env("WRIT_ICECAST_PORT", "8000"))
mp3_mount = env("WRIT_ICECAST_MOUNT", "/next")
lossless_mount = env("WRIT_LOSSLESS_MOUNT", "/lossless")
name = env("WRIT_CALL_SIGN", "WIT-FM")

def call_async(url) =
  thread.run(fun () -> ignore(http.get(timeout=30., url)))
end

def alert(action, key, message) =
  call_async("#{director}/alert?action=#{action}&key=#{url.encode(key)}&message=#{url.encode(message)}")
end

# ── the program ───────────────────────────────────────────────────────────────

def next_request() =
  response = http.get(timeout=20., "#{director}/next")
  if response.status_code == 200 and string.contains(prefix="annotate:", response) then
    request.create(response)
  else
    log.important(label="director", "no request (HTTP #{response.status_code}); falling back")
    null()
  end
end

program = request.dynamic(id="director", prefetch=1, retry_delay=2., timeout=60., next_request)
# One fixed gain per work, from the catalog, via the liq_amplify annotation.
# At unity this multiplies by exactly 1.0 and leaves the samples untouched.
program = amplify(id="work_gain", override="liq_amplify", 1., program)

# The safe library is the catalogued source named "safe" (docs/live-setup.md 6.5).
safe_dir = env("WRIT_SAFE_DIR", "#{library}/air/safe")
ids_dir = env("WRIT_IDS_DIR", "#{library}/ids")
safe = playlist(id="safe_library", mode="randomize", reload_mode="watch", safe_dir)
ids = playlist(id="station_ids", mode="randomize", reload_mode="watch", ids_dir)
# Silence for the first seconds after a start gives the director time to
# answer, so a restart doesn't open with a blip of the safe library. Later
# failures go straight to the safe library.
warmup = source.available(track_sensitive=false, blank(), {time.up() < 5.})
radio = fallback(id="chain", track_sensitive=false, replay_metadata=false, [program, warmup, safe, ids])
radio = mksafe(radio)

def aired(m) =
  path = m["filename"]
  if path != "" then
    call_async("#{director}/aired?entry=#{url.encode(m['witfm_entry'])}&path=#{url.encode(path)}")
  end
end
radio = source.on_track(radio, aired)

def on_silence() =
  log.severe(label="playout", "20 seconds of silence on air")
  alert("send", "silence", "20 s of silence on air (#{mp3_mount}, #{lossless_mount})")
end
def on_sound() =
  alert("clear", "silence", "audio is back on air")
end
# Classical gaps are 2-4 s and quiet passages stay above -60 dBFS, so 20 s
# below it means something is wrong.
radio = blank.detect(id="silence_watch", max_blank=20., threshold=-60., track_sensitive=false,
                     on_noise=on_sound, on_silence, radio)

# ── outputs ──────────────────────────────────────────────────────────────────

# The FLAC branch carries no per-track metadata. In Liquidsoap 2.2.4 (Ubuntu
# 24.04) the Ogg FLAC encoder segfaults when metadata starts a new Ogg chain
# (ocaml-flac's write callback during FLAC__stream_encoder_init_ogg_stream);
# without metadata it ran crash-free and bit-exact in rehearsal. Titles,
# credits and the lossless flag reach players through the now-playing API.
lossless_feed = source.drop.metadata(radio)

if to_files != "" then
  ignore(output.file(id="lossless",
    %ogg(%flac(samplerate=44100, channels=2, compression=5, bits_per_sample=16)),
    "#{to_files}/lossless.ogg", lossless_feed))
  ignore(output.file(id="mp3", %mp3(bitrate=320, samplerate=44100, stereo=true),
    "#{to_files}/next.mp3", radio))
else
  ignore(output.icecast(
    id="lossless",
    %ogg(%flac(samplerate=44100, channels=2, compression=5, bits_per_sample=16)),
    host=host, port=port, password=password, mount=lossless_mount,
    name="#{name} Lossless", genre="Classical",
    description="Lossless FLAC, 16-bit/44.1 kHz. Classical works air at one fixed gain each, unprocessed.",
    lossless_feed))
  ignore(output.icecast(
    id="mp3",
    %mp3(bitrate=320, samplerate=44100, stereo=true),
    host=host, port=port, password=password, mount=mp3_mount,
    name=name, genre="Radio", description="WIT-FM",
    radio))
end
