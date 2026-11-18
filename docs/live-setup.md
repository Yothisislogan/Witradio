# WIT-FM: live setup and Phase 0 runbook

Written November 18, 2026, for Phase 0 of the overhaul (development plan:
"Secure and baseline", Nov 17-23). It covers the server steps the code can't
do: rotating the passwords, snapshotting the server, writing down what
actually runs, and passing the Phase 0 gate.

Section 3 describes the live station **as the repository describes it**. The
server may run changes that were never committed, so every row lists the
command that confirms it on the server.

## 1. Rotate the Icecast passwords first

The old source password was committed to this public repository, and it
stays in git history. Anyone who reads it can connect as a source and take
over a mount if the server still accepts it. Removing it from the code (this
branch) doesn't fix that: **changing it on the server does**.

1. Make two new passwords, one for sources and one for admin:

   ```bash
   openssl rand -base64 24
   ```

2. Find the config file Icecast was started with (`-c <path>`). For the apt
   package it's usually `/etc/icecast2/icecast.xml`; `./writ start icecast`
   uses `config/icecast.xml`.

   ```bash
   ps -o args= -C icecast2 -C icecast
   ```

3. In that file, set new values for `<source-password>`, `<admin-password>`
   and `<relay-password>` if it has one. Make sure none of them is still
   `changeme`, `hackme` or the old committed value.

4. Put the new source password in the environment the station runs in. The
   runtime reads `WRIT_SOURCE_PASSWORD`, which the code already read before
   this branch. To keep it across reboots, store it in an owner-only file:

   ```bash
   mkdir -p ~/.config/witradio && touch ~/.config/witradio/env && chmod 600 ~/.config/witradio/env
   echo "export WRIT_SOURCE_PASSWORD='<new source password>'" >> ~/.config/witradio/env
   ```

   ```bash
   echo '. ~/.config/witradio/env' >> ~/.bashrc && . ~/.config/witradio/env
   ```

   If the station runs under systemd, use `EnvironmentFile=` in the unit
   instead.

5. Restart Icecast, then the stream. A restart drops listeners for a few
   seconds, so pick a quiet moment.

   ```bash
   sudo systemctl restart icecast2
   ```

   ```bash
   ./writ restart stream
   ```

   With this branch merged, `writ` copies `WRIT_SOURCE_PASSWORD` into its tmux
   session and refuses to start a stream when the variable is missing.

6. Check that `/stream` is back, and that the old password is refused. The
   PUT targets a throwaway mount, so it can't disturb `/stream`. **401** means
   the old password is rejected. **200** means it still works, so the
   rotation didn't take effect:

   ```bash
   curl -s http://localhost:8000/status-json.xsl | python3 -m json.tool | grep -E '"listenurl"|"server_name"'
   ```

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 -X PUT -u 'source:<old password>' http://localhost:8000/rotation-check
   ```

## 2. Snapshot the server and tag `v1-legacy`

Commit whatever the server runs that isn't in git, then tag it. The repository
is public, so read the diff before adding anything. Never commit `.env` files,
`config/icecast.xml`, `mac/config.yaml`, API keys, stream keys or audio.

```bash
git status --short
```

```bash
git diff --stat
```

```bash
git switch -c server-snapshot-2026-11
```

```bash
git add <the reviewed files>
```

```bash
git commit -m "Snapshot of the live server before the overhaul"
```

```bash
git push -u origin server-snapshot-2026-11
```

```bash
git tag -a v1-legacy -m "WIT Radio as it ran before the overhaul"
```

```bash
git push origin v1-legacy
```

Merge the snapshot into `main` first, then this branch. If both touched
`config/stations.yaml` or `mac/feeder.py`, keep the snapshot's settings and
this branch's password handling.

## 3. What the live station runs, per the repository

| Area | In the repo | Confirm on the server |
|---|---|---|
| Station | `writ-fm`, call sign "WIT Radio", mount `/stream`, API on :8001. The web player points at port 8000/8001 on the Oracle server's public IP | `curl -s localhost:8001/now-playing` |
| Processes | `writ` runs everything in a tmux session called `writ` | `tmux list-windows -t writ`, `systemctl list-units \| grep -iE 'icecast\|radio\|writ'` |
| Playout | `feeder.py` builds a playlist per show; ezstream encodes Ogg Vorbis q4 to `/stream`; every WAV gets `loudnorm` at -14 LUFS, every FLAC -16 LUFS plus a 3 s fade-in | `pgrep -af 'ezstream\|feeder'` |
| Schedule | `config/schedule.yaml`: four dayparts (Night Shift 00-06, Morning Acoustic 06-09, Day Shift 09-20, Dance Party 20-24), `timezone: local` | `timedatectl`. If the server runs on UTC, every daypart airs 5 hours early in Eastern time: Morning Acoustic at 1-4 AM |
| Hosts | Sunny, Dash, Nova and Nyx (`config/wit_dj_personas.yaml`) on Kokoro voices `am_michael`, `am_onyx`, `af_bella`, `af_heart` | `ls mac/kokoro/.venv` |
| Scripts | `talk_generator.py` → `run_claude` → agent `gemini` → `tools/gemini_prompt_cli.py` (`GEMINI_API_KEY`; `GEMINI_MODEL` defaults to `gemini-1.5-flash`) | `env \| grep GEMINI_MODEL`, `ls -t output/talk_segments/*/` |
| Operator loop | `run_operator.sh` only supports the `claude` and `codex` agents. For `gemini` it exits with "unsupported station agent", so the 15-minute agent loop does nothing | `grep -h unsupported output/operator_session_*.log \| tail -3` |
| How talk gets made | No loop runs it, so probably by hand (`./writ generate talk`) or from cron | `crontab -l`, shell history |
| Music | `music_bumper_generator.py` via music-gen.server (ACE-Step), too slow on Oracle's CPU (`docs/oracle-gemini-pivot.md`). The feeder only plays files in `output/music_bumpers/<show>/` named `<show>_*` or with a sidecar `show_id` | `ls output/music_bumpers/*/ \| head`, `pgrep -af music` |
| Listener messages | `POST /message` → `~/.writ/messages.json`; `listener_daemon.sh` | `tmux list-windows -t writ` |
| YouTube | `mac/youtube_relay.sh` relays the station mount to RTMP | `pgrep -af youtube_relay` |
| Other stations | KLOD-FM on `/klod-fm` (Gemini) and CDEX-FM on `/cdex-fm` (Codex) | the `status-json.xsl` listing |

Write down what you find next to each row. That record is the Phase 0
baseline.

## 4. Problems found in the live chain

A local rehearsal (Icecast 2.4.4, ezstream 1.0.2, the repo's decoder and
encoder settings) found two ways the current chain goes off air. This branch
fixes both, and both fixes apply to `/stream` once merged:

1. **The silence filler disconnects listeners.** The feeder plays a 30 s
   digital-silence file at the end of every music-only playlist, and on a
   loop when a show has nothing stocked. ezstream gets through silent Ogg
   Vorbis far faster than real time, then sends nothing for over 10 s, and
   Icecast drops the source ("Disconnecting source due to socket timeout").
   In an A/B test the playlist with the filler was dropped twice a minute;
   without it, never. The filler is now inaudible room tone (about -87 dBFS),
   which never dropped in repeated runs. Check whether it has been happening
   on the server:

   ```bash
   sudo grep -c "socket timeout" /var/log/icecast2/error.log
   ```

2. **Metadata could stall the stream.** `stream_metadata.sh` ran `uv run
   python`, and ezstream waits for it. If `uv` decides to re-sync the
   project (for example after `pyproject.toml` changes), the stream stalls
   into dead air. It now uses plain `python3`.

Left as they are, since these hosts retire at the Phase 3 cutover:

3. The four dayparts' topic focuses (`morning_reflections`, `workday_vibes`,
   `dance_music`, `night_reflections`) have no topic pools, so the talk
   generator draws from every pool, including CDEX-FM's software topics.
4. The dayparts follow the server's clock (`timezone: local`).
5. KLOD-FM shares WIT Radio's schedule file.

## 5. Start `/next` and pass the Phase 0 gate

The gate: **`/stream` is untouched and playing, `/next` streams a test loop,
and the Halo answers over Tailscale.**

1. Icecast accepts `/next` without extra config, but its `<sources>` limit
   counts every connected mount. With `/stream`, KLOD-FM and CDEX-FM up, a
   limit of 4 leaves room for `/next` only. Raise it to 8 for `/lossless`
   later; `config/icecast.xml.example` has both mounts.

2. Stock the test loop and start the staging station. Each show gets a
   labelled line-up tone, a different pitch per show, so the schedule can be
   heard changing. The helper refuses to touch `/stream`.

   ```bash
   python3 tools/next_test_loop.py
   ```

   ```bash
   ./writ --station wit-next start stream
   ```

3. Check it:

   ```bash
   curl -s localhost:8002/now-playing
   ```

   ```bash
   ffprobe -v error -show_entries stream=codec_name,sample_rate -of csv=p=0 http://localhost:8000/next
   ```

4. Once the Halo serves an OpenAI-compatible endpoint on the tailnet, check
   it from the playout server:

   ```bash
   WRIT_LLM_BASE_URL=http://<halo-tailscale-name>:8080/v1 python3 mac/llm_client.py --station wit-next --health
   ```

5. Clean up the test loop when the real producers start:

   ```bash
   python3 tools/next_test_loop.py --remove
   ```

## 6. What this branch adds for Phase 1

| Piece | File | Try it |
|---|---|---|
| Format-hours grid, pinned to Eastern | `config/wit_next_schedule.yaml` | `python3 mac/schedule.py --schedule config/wit_next_schedule.yaml week` |
| Catalog: licenses, years, lossless flag, one gain per work, airplay audit, permissions log, dispute notes | `mac/catalog.py` | `python3 mac/catalog.py --help` |
| Classical fill planner and rotation rules | `mac/classical_planner.py` | `python3 mac/classical_planner.py --at "2026-11-18 23:00"` |
| One OpenAI-compatible LLM client | `mac/llm_client.py` | `python3 mac/llm_client.py --station wit-next -p "Say hello"` |

The catalog for `wit-next` lives at `~/.writ/stations/wit-next/catalog.db`.
Classical blocks stay locked until it holds 100 h of cleared lossless
classical (Nocturne, Evening Concert), then 200 h (Day Shift) and 300 h
(Matinee, Concert Hall). Until then their slots run Monster Mix.

## 7. Decisions due by the end of Phase 0

From the development plan. The first one decides how the rest of Phase 1 is
built:

1. **Playout engine.** Liquidsoap (recommended: schedules, fallbacks,
   silence detection, several outputs, FLAC with metadata), or keep ezstream
   and `feeder.py` with a second encoder chain for lossless. The planner's
   JSON output (files, gain, gaps, credits) feeds either one.
2. **Playout host.** Stay on Oracle or finish the Hetzner move. Each lossless
   listener uses about 0.3 GB an hour.
3. **Where the Halo lives,** home or office. The less reliable the link, the
   bigger the 48-hour buffer should be.
4. **KLOD-FM and CDEX-FM:** delete their configs, or keep one as a permanent
   test station.
5. **Monthly budget** for Lyria and any paid data feeds.
