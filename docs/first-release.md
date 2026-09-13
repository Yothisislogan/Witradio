# WIT Radio first release

This release updates the existing Oracle broadcast runtime. The tower plays finished audio;
Musicgen remains the generation/review/export project. No AI generation is added to playback.

## What changes

- Music-only playlists loop without an intentional silence tail.
- Empty show pools use `output/backup_music`, restricted to explicitly approved files from this station.
- A background ffmpeg sample checks actual stream audio. Two failed samples trigger a managed encoder restart (at most once per minute). Two silent samples of the same file quarantine that file. A short quiet passage or a single lost sample cannot quarantine a track.
- If both scheduled and approved backup music are absent, the last-resort silence file remains; health reports silent/degraded. Backup audio must be stocked before rollout. No music is generated on the tower.
- The new `dj_break` targets 32–60 words, approximately 15–30 seconds. Rendered audio must be 10–35 seconds. Existing longer WIT segments are skipped. WIT allows at most four short links per rolling hour using persistent play history.
- The four WIT shows use Sunny, Dash, Nova and Nyx from `config/wit_dj_personas.yaml` throughout the generator and API.
- Votes are stored in the station home directory in SQLite (`feedback.db`). One browser has one current vote per track; retrying the same vote is idempotent. Votes can be changed. Unknown/stale tracks and oversized requests are rejected.
- At least three browser identifiers must vote before a track gains or loses weight. Influence is smoothed and bounded to ±25%; repeat protection runs first. This is anonymous browser feedback, not verified unique people, and it is not used to skip the shared broadcast.
- The player joins the live edge, stops downloading on Stop, retries failed playback, and exposes truthful buffering/offline states. Track data expires after 30 seconds. Success is displayed only after the API confirms a vote. Saved tracks are explicitly local to the device.
- Health no longer requires Cloudflare when another reverse proxy provides HTTPS. Set `WRIT_REQUIRE_TUNNEL=1` only when the deployment actually requires cloudflared.

## Rollout on the existing tower

1. Record the currently deployed commit and how its streamer is supervised (`./writ`, systemd, or another process manager). Preserve the private station configuration, source password, audio directories, and station home. Pull this release into the existing checkout after review.
2. Confirm `ffmpeg`, `ffprobe`, `ezstream`, `oggenc`, Python and the existing runtime dependencies are available. Run the automated checks below before restarting.
3. Import a few hours of finished, approved music. For example, from the repository root:

   ```bash
   python3 tools/import_backup.py --station writ-fm /path/to/approved/music/*.mp3
   ```

   The importer fully decodes files, rejects silent/unreadable/rejected/cross-station audio, and publishes copies atomically. It does not generate music or alter the originals. Other accepted formats: WAV, FLAC and OGG. Back up the station home containing history and votes.
4. Use the server's intended local timezone for show boundaries. `config/schedule.yaml` currently uses local time; this release does not change the server timezone. Confirm 06:00 Morning Acoustic, 09:00 The Day Shift, 20:00 Dance Party, and 00:00 The Night Shift on the tower.
5. Configure the public player, stream and API on one HTTPS origin. `config/Caddyfile.example` provides the routing; replace the example hostname and filesystem root with the actual deployment. Route `/stream` to Icecast and `/api/*` to the station API. `web/player-config.js` uses those paths. If embedding on another site, set both configuration URLs to the deployed HTTPS origin. Do not merely replace `http` with `https` on an IP address that has no TLS certificate.
6. Restart the **existing** streamer through its current supervisor. For a deployment managed by the repository CLI:

   ```bash
   ./writ restart stream
   ```

   Do not start a second competing encoder. Do not start the heavy music generation stack on the Oracle CPU server. No generation/operator service is enabled by this release.
7. Listen through the public player. Check `/api/health` for fresh `components.audio` results, correct show/host/track information, a saved vote, and recent music history. Play, stop, reconnect, and test on a phone. The first audio sample may take about 20 seconds; steady sampling is every 30 seconds plus sample time.
8. In a staging copy, use an empty show music directory with an approved backup pool; verify audible backup playback. Verify recovery from a stopped encoder and a silent source without modifying production music during the check.

The code and automated fixtures alone do not establish that the public station has been deployed or is audible. Deployment requires access to the Oracle host, its supervisor and the chosen domain/TLS configuration.

## Checks

```bash
python3 -m unittest discover -s tests -v
node --check web/player.js
node tests/player-state.cjs
node tests/player-browser.cjs
```

The browser check uses local fixtures, Playwright and a temporary HTTP server. It exercises real browser audio playback, voting failure/success, stale metadata, stop/rejoin behavior and mobile layout. It does not contact the live radio tower.

## Recovery and rollback

- Quarantined file paths and reasons are in `output/runtime/quarantine.json`. Inspect/fix the audio before removing its entry. Imports and quarantine are station-scoped.
- Keep the prior commit available. To roll back, stop the managed streamer, restore the prior code and player configuration, then restart with the same private configuration and audio directories. The new SQLite vote database may remain on disk; older code ignores it.
- Do not delete audio, vote history or private configuration to roll back a code release.

## Validation at handoff

- 50 Python tests passed, including real ffmpeg audio probes and HTTP feedback requests.
- Dependency-free JavaScript state checks passed for playback intent, failed/saved votes, favorites, stop/rejoin, stale metadata, retry exhaustion and unavailable browser storage.
- JavaScript syntax and git whitespace checks passed.
- Full browser QA is pending: this workspace has no installed Chromium executable, and the provided preview browser blocks the local test address. The browser CI workflow is included but has not run.
- Publication was blocked by automatic approval review because pushing the release would disclose changes to a public repository without explicit publication approval. No remote branch/PR or live deployment was created.
