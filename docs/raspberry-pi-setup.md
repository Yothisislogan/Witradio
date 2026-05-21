# WIT Radio Raspberry Pi Setup

## Recommended Hardware

- Raspberry Pi 5, preferably 8GB RAM
- USB SSD or NVMe storage
- Ethernet connection
- quality USB-C power supply
- small case with cooling

## Recommended Architecture

Use the Pi as the broadcast appliance.

The Pi should run:

- Icecast
- ffmpeg
- Python scheduler
- playlist rotation
- now-playing API
- web player
- Cloudflare tunnel

AI generation should happen elsewhere first.
The Pi should only play finished audio files.

## Install Raspberry Pi OS

Use Raspberry Pi Imager and choose Raspberry Pi OS Lite 64-bit.
Enable SSH during image setup.
Use Ethernet if possible.

## First Boot

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git python3 python3-venv python3-pip ffmpeg icecast2 ezstream tmux curl
```

## Clone Repos

```bash
mkdir -p ~/wit-radio
cd ~/wit-radio
git clone https://github.com/Yothisislogan/Witradio.git
git clone https://github.com/Yothisislogan/Musicgen.git
```

## Create Music Folders

```bash
cd ~/wit-radio/Witradio
mkdir -p music/morning_acoustic music/day_shift music/dance_party music/night_shift runtime
```

## Export Approved Music From Musicgen

```bash
cd ~/wit-radio/Musicgen
python3 tools/export_to_witradio.py ../Witradio
```

## Build Playlists

```bash
cd ~/wit-radio/Witradio
python3 tools/build_playlist.py morning_acoustic
python3 tools/build_playlist.py day_shift
python3 tools/build_playlist.py dance_party
python3 tools/build_playlist.py night_shift
bash tools/rotate_playlist.sh
```

## Test Now Playing API

```bash
python3 tools/update_now_playing.py "The Day Shift" "Test Track" "WIT AI Music Lab" "Dash"
python3 tools/now_playing_server.py
```

Open:

```text
http://PI_IP_ADDRESS:8787/now-playing
```

## Icecast Notes

Configure Icecast with a `/witradio` mount and matching source password.

Local stream target:

```text
http://PI_IP_ADDRESS:8000/witradio
```

## What Not To Run On The Pi Initially

Avoid running heavy AI generation locally at first:

- full AI music generation
- large local language models
- realtime TTS at scale
- video rendering or OBS

Generate audio elsewhere, approve it, then copy it to the Pi.

## Best MVP Test

The first success milestone is:

- Pi boots reliably
- Icecast runs
- playlist exists
- now-playing API works
- web player can connect
- stream plays finished audio continuously
