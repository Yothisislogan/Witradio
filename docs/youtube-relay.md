# WIT Radio YouTube Relay

## Goal

Relay the Icecast stream to YouTube Live for additional reach.

## Recommended Flow

```text
WIT Radio Runtime
    ↓
Icecast
    ↓
ffmpeg relay
    ↓
YouTube RTMP
```

## Example Relay Command

```bash
ffmpeg \
-re \
-i http://localhost:8000/witradio \
-c:a aac \
-b:a 192k \
-f flv \
rtmp://a.rtmp.youtube.com/live2/YOUR_STREAM_KEY
```

## Optional Background Image

Use a static image or looping video as the background for the YouTube livestream.

Recommended branding:

- WIT monster mascot
- Now playing section
- rotating show schedule
- blue neon theme (#00AEEF)

## Recommended Tools

- ffmpeg
- OBS Studio
- cloudflared
- Stream Deck (optional)

## Recommended Future Features

- auto-generated visuals
- waveform visualizer
- AI-generated album art
- auto-updating now playing overlay
- live listener chat integration
