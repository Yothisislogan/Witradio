# WIT Radio DigitalOcean Deployment

## Goal

Use DigitalOcean as the always-on public broadcast server for WIT Radio.

The cloud server should run:

- Icecast
- ffmpeg or ezstream
- Python scheduler
- playlist rotation
- now-playing API
- web player files
- stream monitoring

AI music generation can happen elsewhere and upload finished audio to the server.

## Step 1: Create Droplet

Recommended MVP Droplet:

- Ubuntu 24.04 LTS
- 2 vCPU
- 4GB RAM
- 80GB SSD or larger
- SSH key login

## Step 2: SSH Into Server

```bash
ssh root@YOUR_SERVER_IP
```

## Step 3: Install Packages

```bash
apt update
apt upgrade -y
apt install -y git python3 python3-venv python3-pip ffmpeg icecast2 ezstream tmux curl nginx ufw
```

## Step 4: Open Firewall

```bash
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw allow 8000
ufw enable
```

## Step 5: Clone WIT Radio

```bash
mkdir -p /opt/witradio
cd /opt/witradio
git clone https://github.com/Yothisislogan/Witradio.git .
```

## Step 6: Create Music and Runtime Folders

```bash
mkdir -p music/morning_acoustic music/day_shift music/dance_party music/night_shift runtime logs
```

## Step 7: Upload Finished Audio

Upload approved MP3, WAV, or OGG files into the matching show folders.

Example folders:

```text
music/morning_acoustic
music/day_shift
music/dance_party
music/night_shift
```

## Step 8: Build Playlists

```bash
python3 tools/build_playlist.py morning_acoustic
python3 tools/build_playlist.py day_shift
python3 tools/build_playlist.py dance_party
python3 tools/build_playlist.py night_shift
bash tools/rotate_playlist.sh
```

## Step 9: Configure Icecast

Copy or adapt:

```text
config/icecast-witradio.xml.example
```

Make sure source passwords are changed before going public.

## Step 10: Start Icecast

```bash
systemctl enable icecast2
systemctl restart icecast2
```

Check:

```text
http://YOUR_SERVER_IP:8000
```

## Step 11: Start Now Playing API

```bash
python3 tools/now_playing_server.py
```

Check:

```text
http://YOUR_SERVER_IP:8787/now-playing
```

## Step 12: Start Playlist Rotation

```bash
python3 tools/schedule_daemon.py
```

## Step 13: Start Streaming

Use ezstream or the WRIT runtime to stream:

```text
runtime/current_playlist.m3u
```

to Icecast mount:

```text
/witradio
```

## Step 14: Point Domain

Recommended DNS:

```text
radio.weinsurethings.com -> YOUR_SERVER_IP
```

Use Nginx or Cloudflare to serve the public web player.

## Step 15: First Success Milestone

The MVP is successful when:

- Icecast status page is reachable
- /witradio stream plays audio
- playlists rotate by schedule
- now-playing API returns JSON
- web player can play the public stream
- server can run for several hours without dead air

## Important Rule

Do not make the live broadcast wait on AI generation.
Only finished, approved audio should be played by the live cloud server.
