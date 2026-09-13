# WIT Radio

Instrumental music and short hosted breaks from We Insure Things, throughout the day.

WIT Radio runs the public broadcast tower: Icecast, playlist scheduling, metadata,
a web player, listener feedback, and recovery. **Musicgen handles music generation,
review, tagging and export of finished audio.** The Oracle CPU server must not wait
for live AI music generation.

## Current release

The [first release guide](docs/first-release.md) covers installation, backup audio,
HTTPS routing, verification and rollback. It also distinguishes tested repository
behavior from deployment on the live tower.

- Approved backup music replaces empty show pools. Music playlists have no silence tail.
- Actual audio sampling detects silence and unavailable streams; recovery is independent of AI generation.
- DJ links target 15–30 seconds with a 35-second rendered ceiling and a four-link hourly budget.
- Server-side listener votes influence subsequent music rotation with bounded weight and repeat protection.
- The player prioritizes **Play Live**, reports accurate connection states, expires stale metadata, and supports saved tracks, recent music and upcoming shows.

## Shows and voices

The [daily schedule](SCHEDULE.md) is defined in `config/schedule.yaml`.
`config/wit_dj_personas.yaml` defines Sunny, Dash, Nova and Nyx for the generator and API.
The server’s local timezone determines show boundaries.

## Runtime

Musicgen exports finished audio to the WIT libraries. The feeder schedules files,
ezstream sends them to Icecast, and the HTTPS player reads the stream and metadata API.

The `writ` CLI manages existing tmux-based installations:

```bash
./writ status
./writ logs stream -f
./writ restart stream
```

Use the actual existing supervisor if the tower uses systemd or another manager.
Do not start a second competing encoder. A restart is needed to load updated Python code.

For an already provisioned tower, import finished music approved for backup broadcast:

```bash
python3 tools/import_backup.py --station writ-fm /path/to/approved/music/*.mp3
```

The importer validates complete audio before publishing copies. It rejects silence,
undecodable files, rejected metadata and audio assigned to another station. Stock a
few hours of backup music before enabling recovery; absent all music, the station
still has a last-resort silence file and reports degraded health.

## Public player and API

`web/wit-music-player.html` is the canonical player. Its configuration is
`web/player-config.js`. Serve it on the same HTTPS origin as `/stream` and `/api/*`.
`config/Caddyfile.example` provides reverse-proxy routing; configure the actual
hostname, DNS, filesystem path and TLS on the host before using it publicly.

Key endpoints on the station API (normally port 8001):

| Endpoint | Purpose |
|---|---|
| `GET /now-playing` | Current track, stable track ID, show, host and audio health |
| `GET /schedule` | Current and upcoming shows |
| `GET /history` | Recently played tracks |
| `GET /health` | Component and actual-audio status |
| `POST /feedback` | Persist a like/dislike for a recently aired track |
| `POST /message` | Existing listener message queue |

Feedback accepts `track_id`, `listener_id` (browser UUID), and `vote` (`like` or
`dislike`). The server owns track identity and eligibility. A confirmed saved vote
is required before the player shows success. Favorite tracks are stored locally
and are labeled “On this device.”

## Configuration and dependencies

- `config/stations.yaml`: station identity, Gemini/Codex command, ports, paths and stream mounts.
- `config/schedule.yaml`: the four WIT shows and short-break segment type.
- `config/wit_dj_personas.yaml`: WIT voices and speaking style.
- Private server configuration and `WRIT_SOURCE_PASSWORD` remain outside commits.
- Python 3.11+, PyYAML and the existing runtime dependencies; ffmpeg/ffprobe, ezstream,
  vorbis-tools and Icecast are required for broadcasting. Kokoro is used where spoken
  audio is rendered. A reverse proxy supplies public HTTPS.

The runtime retains isolated station instances (`writ-fm`, `klod-fm`, `cdex-fm`).
The existing `writ-fm` identifier and `/stream` mount are retained for compatibility.

```bash
./writ --station writ-fm status
./writ --station cdex-fm status
```

## Verification

```bash
python3 -m unittest discover -s tests -v
node --check web/player.js
node tests/player-state.cjs
node tests/player-browser.cjs
```

The browser test requires Playwright and its Chromium browser. It uses local fixture
audio and API responses; it never tests or changes the public broadcast.

## Origins and license

WIT Radio builds on [WRIT-FM](https://github.com/keltokhy/writ-fm). MIT license.
