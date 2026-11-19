# WIT-FM: live setup, Phase 0 and Phase 1 runbook

Written November 18, 2026, for Phase 0 of the overhaul (development plan:
"Secure and baseline", Nov 17-23). It covers the server steps the code can't
do: rotating the passwords, snapshotting the server, writing down what
actually runs, and passing the Phase 0 gate. Section 6 covers Phase 1: the
Liquidsoap playout, email alerts and stocking the music library, including
the Library of Congress recordings for The Stacks. Section 7 covers the
Halo, which writes and voices the shows.

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

## 6. Phase 1: the Liquidsoap playout

Decided November 18: the playout runs on Liquidsoap. One 16-bit/44.1 kHz
master feeds two outputs, encoded in parallel: `/lossless` (Ogg FLAC) and
`/next` (MP3 320 kbps; `/stream` at the cutover). The YouTube relay reads
`/next`.

| Piece | File | Try it |
|---|---|---|
| Format-hours grid, pinned to Eastern | `config/wit_next_schedule.yaml` | `python3 mac/schedule.py --schedule config/wit_next_schedule.yaml week` |
| Catalog: licenses, years, lossless flag, one gain per work, airplay audit, permissions log, dispute notes | `mac/catalog.py` | `python3 mac/catalog.py --help` |
| Music pull from the bible's cleared sources | `tools/pull_music.py`, `config/music_sources.yaml` | `python3 tools/pull_music.py sources` |
| Library of Congress intake for The Stacks, with the content screen | `tools/pull_loc.py`, `config/loc_sources.yaml` | `python3 tools/pull_loc.py sources` |
| Classical fill planner and rotation rules | `mac/classical_planner.py` | `python3 mac/classical_planner.py --at "2026-11-18 23:00"` |
| The Stacks planner: hour clock, theme carts, rotation | `mac/stacks_planner.py` | `python3 mac/stacks_planner.py --at "2026-11-19 13:00"` |
| Playout master: outputs, gain, fallbacks, silence detection | `liquidsoap/wit_next.liq` | `./writ --station wit-next start playout` |
| Director: what plays next, now-playing, airplay log | `mac/playout_director.py` | `python3 mac/playout_director.py status` |
| Alerts by email (text messages optional) | `mac/alerts.py` | `python3 mac/alerts.py test` |
| Outside monitor and listener log | `mac/playout_monitor.py` | `python3 mac/playout_monitor.py once` |
| One OpenAI-compatible LLM client | `mac/llm_client.py` | `python3 mac/llm_client.py --station wit-next -p "Say hello"` |

The catalog for `wit-next` lives at `~/.writ/stations/wit-next/catalog.db`.
Classical blocks stay locked until it holds 100 h of cleared lossless
classical (Nocturne, Evening Concert), then 200 h (Day Shift) and 300 h
(Matinee, Concert Hall). Until then their slots run Monster Mix. The Stacks
unlocks at 15 approved records (section 7.6).

### 6.1 Install and start

1. Install Liquidsoap 2.2 or newer. Ubuntu 24.04 packages 2.2.4:

   ```bash
   sudo apt install liquidsoap
   ```

2. Raise Icecast's `<sources>` to 8 and add the `/lossless` mount, as in
   `config/icecast.xml.example`.

3. Run the gate test against the installed Liquidsoap (section 6.3).

4. Start the playout. `playout` runs Liquidsoap and the director together,
   and restarts the director if it dies. It refuses to start while the
   Phase 0 test loop (`stream`) holds `/next`, so stop that first:

   ```bash
   ./writ --station wit-next stop stream
   ```

   ```bash
   ./writ --station wit-next start playout
   ```

   ```bash
   ./writ --station wit-next start station-api
   ```

   ```bash
   ./writ --station wit-next start monitor
   ```

5. Check it:

   ```bash
   ./writ --station wit-next logs playout
   ```

   ```bash
   curl -s localhost:8002/now-playing
   ```

### 6.2 What plays, and what happens when something fails

Liquidsoap asks the director for every item over localhost HTTP (port
8102: the station API port plus 100). In a classical block the director
plays the planner's plan: each work at its catalog gain, movements back to
back, 2-4 s of digital silence between works, a station ID at the hour,
and the Count's links from `buffer/talk/<plan>/`, which the Halo writes and voices (section 7).
Running late, it drops the fit pieces and won't start a work that would
overrun the post by more than a minute. If the plan runs short, it fills
with a one-movement classical work that ends before the post, else a
safe-library track that fits, else inaudible room tone. Monster
Mix, and classical blocks still under their hours, play the show's buffer
(`buffer/<show>/<slot>/`), then the safe library.

If the director stops answering, Liquidsoap falls back on its own: safe
library (`library/air/safe`), then station IDs (`library/ids`), then
silence.
After 20 s below -60 dBFS it sends an alert email, and another when audio
returns.
The monitor watches from outside: both mounts on Icecast, the program
moving, an 8-second silence sample of `/next`, the buffer under
`WRIT_BUFFER_ALERT_HOURS` (default 12, once it has filled) and, when
`WRIT_LLM_BASE_URL` is set, the Halo. It logs listener counts per mount
and show to `runtime/playout/listeners.db`.

To rehearse classical blocks on `/next` before the catalog has the hours,
start the playout with `WRIT_FORCE_CLASSICAL=1` in the shell. `writ`
clears it again on a start without it.

### 6.3 The lossless branch and the gate test

A FLAC at unity gain comes out of `/lossless` bit for bit. There is one
exception: Liquidsoap carries samples as floats, so the value -32768 airs
as -32767 (1 LSB, at negative full scale). Works whose catalog gain isn't
0 dB air at that one fixed gain, with no fades, compression or limiting.

The FLAC stream carries no per-track titles. Liquidsoap 2.2.4's Ogg FLAC
encoder segfaults when a title change starts a new Ogg chain. Titles,
credits, license and a lossless flag reach the player and apps through
the now-playing API, which the director writes as each item starts. The
MP3 mount keeps its stream titles.

The gate test runs the real `liquidsoap/wit_next.liq` against the
installed Liquidsoap, writing to files, for about 35 s. It checks the
bit-for-bit unity work, the -6 dB gain to within 1 LSB, exact gap lengths,
movements back to back, fallback to the safe library, the director
callbacks, and that sixteen short titled tracks don't crash the encoder.
Run it on the playout host after any Liquidsoap install or upgrade:

```bash
python3 -m pytest tests/test_playout_liquidsoap.py -v
```

Icecast passes the Ogg pages through untouched: in rehearsal, a recording
of `/lossless` from Icecast matched the source file's SHA-256.

### 6.4 Alerts by email

The playout emails when something goes wrong: 20 s of silence, a mount off
Icecast, the program stalled, the buffer running low, or the Halo not
answering. Each problem sends at most one email every 30 minutes, then one
"Resolved" email when it clears.

Set these where `writ` is started from, never in the repository:

| Variable | Value |
|---|---|
| `WRIT_ALERT_EMAIL_TO` | Who gets the alerts; several addresses separated by commas |
| `WRIT_SMTP_HOST` | Your mail provider's SMTP server, e.g. `smtp.gmail.com` |
| `WRIT_SMTP_PORT` | `587` (the default), or `465` for SSL |
| `WRIT_SMTP_USER` | The account that sends, ideally a mailbox just for alerts |
| `WRIT_SMTP_PASSWORD` | Its password; for Gmail, an app password |
| `WRIT_SMTP_FROM` | The sender address, if it isn't `WRIT_SMTP_USER` |

With Gmail, turn on 2-Step Verification for the sending account, create an
app password, and use `smtp.gmail.com` on port 587. A sending service such
as SendGrid, Mailgun or Amazon SES works too: each gives you an SMTP host,
user and password. Send from an address the provider is allowed to send
for, or the alerts may land in spam.

Send a test:

```bash
python3 mac/alerts.py --station wit-next test
```

Without these settings, alerts only go to `runtime/playout/alerts.jsonl`.
Text messages through Twilio still work alongside email if its settings
are present (`mac/alerts.py` lists them), but US carriers only deliver them
once the number is registered (A2P 10DLC or toll-free verification).

### 6.5 Stocking the library

1. **Safe library.** Everything that airs must be catalogued: the airplay
   audit (`python3 mac/catalog.py audit`) flags any file that isn't, and a
   clean audit is a Phase 3 gate. Add the generated safe tracks through the
   catalog as the source named `safe`. Their air copies land in
   `library/air/safe/`, where the director and Liquidsoap read them:

   ```bash
   python3 tools/pull_music.py local ~/safe-tracks --source safe --kind generated --license generated --credit "Generated for WIT-FM"
   ```

   The same rule will apply to the Phase 2 producers: tracks they put in
   the buffer need catalog entries. Station IDs in `library/ids/` and
   talk are not logged as airplay.

2. **The bible's Internet Archive sets.** List them, see what each holds,
   then fetch:

   ```bash
   python3 tools/pull_music.py sources
   ```

   ```bash
   python3 tools/pull_music.py fetch all --dry-run
   ```

   ```bash
   python3 tools/pull_music.py fetch all
   ```

   ```bash
   python3 tools/pull_music.py report
   ```

   They come to about 35 hours: Open Goldberg Variations 1.4 h, Open
   Well-Tempered Clavier Book 1 1.8 h, Kimiko Ishizaka's Art of the Fugue
   1.3 h, Musopen's Chopin 13.6 h and the Musopen Kickstarter set 16.7 h.
   Each download is checked against the Archive's MD5. Each file gets a
   spectrogram check for fake lossless, and each work one gain (EBU R128
   across its movements, toward -18 LUFS, capped by true peak). Masters are
   kept; the air copies are 16-bit/44.1 kHz FLAC.

   Some works arrive quarantined until someone checks them. The Art of the
   Fugue set's license needs confirming, and 40 Chopin works (1.95 h) look
   posthumously published. The report in `library/reports/<source>.yaml`
   lists each work with its reason. It also notes the Kickstarter set's
   Borodin quartets, credited to an orchestra, and Suk (died 1935). Fix
   details with `python3 mac/catalog.py edit`, then put a checked source on
   air:

   ```bash
   python3 tools/pull_music.py approve musopen-chopin --reason publication
   ```

3. **Musopen site downloads and Marine Band CDs** make up the rest of the
   first 100 hours. Add a folder of lossless files per source, for example:

   ```bash
   python3 tools/pull_music.py local ~/rips/marine-band --source marine-band --license public_domain --credit "United States Marine Band" --grouping album --dry-run
   ```

   Drop `--dry-run` once the works look right. Recordings must be published
   in 1925 or earlier, or carry a CC0 or public-domain grant (Musopen,
   Marine Band). Compositions must date from 1930 or earlier. The intake
   checks both and quarantines what it can't confirm.

### 6.6 The Stacks: Library of Congress recordings

`tools/pull_loc.py` brings in the 100-year-old recordings The Stacks plays.
The sources are listed in `config/loc_sources.yaml`:

| Source | What it holds | Runs |
|---|---|---|
| `loc-edison` | Edison disc recordings, full length (about 80) | by default |
| `loc-variety-stage` | Vaudeville-era Edison recordings, full length | by default |
| `cdj-jukebox` | Citizen DJ's clips from National Jukebox records published before 1923, 4 to 30 s each: remix material | by default |
| `loc-jukebox` | Full National Jukebox recordings as WAV masters (about 20,000 from 1900 to 1925) | only with `--opt-in` |

Screen a source first, then bring items in. Each run takes 25 items unless
`--limit` says otherwise, and a rerun carries on where the last one stopped:

```bash
python3 tools/pull_loc.py fetch loc-edison --dry-run
```

```bash
python3 tools/pull_loc.py fetch loc-edison --limit 25
```

```bash
python3 tools/pull_loc.py fetch cdj-jukebox --collection loc-jukebox-jazz --limit 10
```

`--query` searches within a source, which suits the theme carts:

```bash
python3 tools/pull_loc.py fetch loc-edison --query automobile
```

Every item passes three checks before anything downloads:

1. **Publication year.** "No year, no airplay." Edison records carry their
   publication year. The Jukebox gives only the session date, so a
   recording made in 1924 or earlier counts as published by 1925. A 1925
   recording comes in with no year, because it may have been issued in
   1926. Later recordings are skipped.
2. **Issue number.** A label and catalog number show the record was
   published. Without one, the item comes in with no year.
3. **Content screen.** Items in the Jukebox's "Ethnic characterizations"
   genre, or whose titles use slurs, stay out and are never downloaded.
   Humorous songs, comedies and a list of warning words are flagged. Tune
   both lists in `config/loc_sources.yaml`.

Everything that comes in is quarantined until someone listens to it. That
is the Phase 4 gate: nothing the reviewer would exclude reaches air.

```bash
python3 tools/pull_loc.py review
```

```bash
python3 tools/pull_loc.py approve 41 42 43 --by Logan
```

```bash
python3 tools/pull_loc.py reject 44 --why "dialect comedy" --by Logan
```

For an item with no year, look up its release date in the Discography of
American Historical Recordings (adp.library.ucsb.edu). Set the year, then
approve it with `--reason publication`:

```bash
python3 mac/catalog.py edit 45 --year 1925
```

```bash
python3 tools/pull_loc.py approve 45 --reason publication --by Logan
```

`python3 tools/pull_loc.py excluded` lists what the screen kept out, with
loc.gov links. To review one of those anyway, rerun the fetch with
`--include <item id>`.

loc.gov allows 20 requests a minute and blocks for an hour past that. Any
request during the block restarts the hour. The script makes one request
every 3.5 s and caches item records. On a 429 or a CAPTCHA page it stops
and says to wait an hour. The host needs to reach `www.loc.gov`,
`tile.loc.gov`, `s3.amazonaws.com` and `raw.githubusercontent.com`.

**The National Jukebox is your call.** The bible says to use Citizen DJ's
files instead of the Jukebox, because the Jukebox streams are licensed from
Sony and couldn't be downloaded. As of November 2026, loc.gov serves WAV
masters for Jukebox items. Its rights statement says recordings published
before 1923 have been public domain since 2022, and 1923-1925 recordings
since 2024-2026. loc.gov offers 1926 records for download too, so its
download flags don't track copyright: the script's year check does.
`loc-jukebox` stays opt-in until you decide.

Approved items go on air through The Stacks (section 7.6).

## 7. The Halo: writing and voicing the shows

All the AI runs locally on the Halo: the script model, the voices and the
music. The Halo makes each show ahead of air and copies the finished audio
to the playout server on Hetzner over Tailscale.

### 7.1 What to run on the Halo

| Piece | What | Setting |
|---|---|---|
| Script model | Any OpenAI-compatible server: llama.cpp (Vulkan first), Lemonade, LM Studio or Ollama | `WRIT_LLM_BASE_URL` (e.g. `http://127.0.0.1:8080/v1`) and `WRIT_LLM_MODEL` |
| Voices | Each engine in `config/voices.yaml` behind a server that speaks `POST /v1/audio/speech` and returns WAV, on the ports listed there, or a command that writes a WAV. Kokoro (`mac/kokoro/`) is every host's fallback | Reference clips in `WRIT_VOICES_DIR` (default `~/.writ/voices`) |
| Music | music-gen.server with ACE-Step, already used by the old station. Lyria isn't needed, since everything runs locally | `MUSIC_GEN_URL` (default `http://localhost:4009`) |

Check the engines and audition a host:

```bash
python3 mac/voices.py check
```

```bash
python3 mac/voices.py say --host count "Good evening, night creatures. The world sleeps. We do not."
```

### 7.2 Start the producers

```bash
./writ --station wit-next start producers
```

Each cycle (every 5 minutes) pulls the server's plans, makes whatever is
due, and pushes the audio:

| Show | Made | Notes |
|---|---|---|
| The Count's classical links | 36 h ahead, from the server's plans | A link that fails the fact check becomes a plain announcement from the plan |
| The Market | 2 h ahead; The Tape and this morning's releases 20 min ahead | Fails closed; every episode ends with the disclaimer |
| Ying & Yang | 2 h ahead | Fails closed; logs a prediction, grades it from the scores, reads the Receipts |
| Monster Mix | 12 h ahead | The Drift plus Wit's breaks; also fills classical slots that haven't unlocked; at most `WRIT_MONSTER_MIX_TRACKS_PER_HOUR` new tracks (default 8) |
| The Stacks | 36 h ahead, from the server's plans | Steve's talk, the remixes, speech samples and Then & Now (section 7.6) |
| Miles, Office Hours, Bedtime Stories | Not built yet | Their slots play the safe library |

To hear a show without touching the buffer, rehearse one airing. It lands
in `runtime/rehearsal/`:

```bash
python3 mac/producer_daemon.py show market_closing_tape --at "2026-11-18 16:30" --rehearse
```

### 7.3 Scripts and the fact check

The script model gets the house rules and each host's brief
(`config/prompts/`) as its system prompt, and the fetched facts as the only
source of facts. It replies with a voice script (speaker, text, emotion,
intensity, pace, nonverbal, pauses). Every number and proper name must be in
the facts; misses go back to the model, and a script that still misses is
dropped. The repository is public, so to keep the host briefs private, copy
them elsewhere and set `WRIT_PROMPTS_DIR`.

Hard names are respelled for the voices from `config/pronunciations.yaml`.
The Count's anecdotes may only use facts from the plan and from a composer
fact file, `config/composers/<composer-name>.txt`, one fact per line (dates,
premieres, places). None exist yet.

### 7.4 Sports and market data

Try the feeds:

```bash
python3 mac/sports_feed.py
```

```bash
python3 mac/market_feed.py --format closing_tape
```

| Feed | Provider | Key | Cleared for air |
|---|---|---|---|
| Scores, schedules, headlines | ESPN's public site API | none | No: unofficial, for rehearsal |
| Treasury yields (same afternoon) | U.S. Treasury | none | Yes |
| Oil, CPI, jobs, retail sales, release calendar | FRED | free, `FRED_API_KEY` | Yes |
| S&P 500, Dow, Nasdaq, mortgage rates | FRED, republishing their owners' data | same | No, until licensed |
| 8:30 AM releases | BLS | free, `BLS_API_KEY` (keyless allows 25 requests a day) | Yes |
| What the Fed said | Federal Reserve Board press releases | none | Yes |

`config/feeds.yaml` sets `cleared_for_air` per provider. Ying & Yang and
any Market segment built on an uncleared provider only rehearse. Going on
air takes a licensed scores feed (SportsDataIO, Goalserve, Sportradar) and
a licensed source of index closes (for example Intrinio's end-of-day index
levels, sold with display rights). Pick them and I'll add them as providers.
The Halo needs to reach `site.api.espn.com`, `api.stlouisfed.org`,
`home.treasury.gov`, `api.bls.gov` and `www.federalreserve.gov`.

### 7.5 Getting the audio to Hetzner

1. Put both machines on Tailscale, and give the Halo an SSH key that can
   log in to the playout server.
2. On the Halo, point the sync at the server's buffer:

   ```bash
   export WRIT_SYNC_REMOTE=witfm@<hetzner-tailscale-name>:/home/witfm/Witradio/output/stations/wit-next/buffer
   ```

3. The server plans the classical and Stacks blocks 36 hours ahead on its
   own (the playout runs a planner every 15 minutes) and writes
   `buffer/plans/_status.json`, saying which blocks have unlocked. The Halo
   pulls those and pushes finished audio; files land whole, so the playout
   never sees half a segment.
4. Prune old slots on both machines, for example from cron:

   ```bash
   python3 mac/buffer_sync.py prune --days 3
   ```

If the Halo goes dark, the server plays what's already in its buffer, then
the safe library. That is the Phase 1 gate's "4 hours with the Halo switched
off".

### 7.6 The Stacks

The playout server plans each Stacks block from the approved Library of
Congress records, on the bible's clock: Pull One, two records with an intro
each, a remix, Call Number, more records and remixes, the Speech Sample near
:40, Then & Now near :45, and Back to the Stacks before the hour. Each hour
takes one theme cart; Saturday's Crate Dig keeps one cart for both hours and
remixes every pair. No record repeats within 14 days, counting what's
already planned. Preview a block:

```bash
python3 mac/stacks_planner.py --at "2026-11-19 13:00"
```

The records play straight from the server's library. The Halo renders the
rest into `buffer/talk/<plan>/`:

| Segment | How it's made |
|---|---|
| Steve's talk | Written from the plan's record facts (year, performer, label, the Library's notes). A script that fails the fact check becomes a plain announcement from those facts |
| The Remix | A phrase from the record, or from its Citizen DJ clip; its tempo and key (librosa); a new instrumental bed from ACE-Step at that tempo and key; the phrase stretched onto the bar lines with the bed ducked under it (`mac/remix.py`). Bed styles rotate through boom-bap, lo-fi, house, electro-swing and trip-hop |
| Speech Sample | The recording over an ambient bed, faded out at 4 minutes |
| Then & Now | A new ACE-Step track in the original's feel, right after it |

A remix or speech sample carries the recording's credit in now-playing, and
the director logs the recording's airplay. Before rendering, the Halo copies
the recordings it re-cuts from the server's library into its own
(`WRIT_LIBRARY_DIR`). By default it looks for the server's library next to
the buffer in `WRIT_SYNC_REMOTE`; set `WRIT_SYNC_LIBRARY_REMOTE` if the
library lives elsewhere. librosa is a project dependency; without it, beds
use their style's own tempo and phrases aren't stretched.

The Stacks unlocks once the catalog holds 15 approved records
(`unlock_records` in `config/wit_next_schedule.yaml`). Until then its slots
run Monster Mix. To stage it on `/next` sooner, start the playout with
`WRIT_FORCE_STACKS=1`.

## 8. Decisions due by the end of Phase 0

From the development plan:

1. **Playout engine.** Decided November 18: Liquidsoap (section 6).
2. **Playout host.** Decided: the Hetzner server. Each lossless listener uses
   about 0.3 GB an hour.
3. **Where the Halo lives,** home or office. The less reliable the link, the
   bigger the buffer should be.
4. **KLOD-FM and CDEX-FM:** delete their configs, or keep one as a permanent
   test station.
5. **Monthly budget** for a licensed scores feed and licensed market closes
   (all AI runs locally, so there's no Lyria bill).
