# WIT Radio Testing Checklist

## 1. Verify Homebrew Packages

```bash
brew install ffmpeg icecast ezstream vorbis-tools tmux
```

## 2. Start Icecast

```bash
icecast -c /opt/homebrew/etc/icecast.xml
```

Open:

```text
http://localhost:8000
```

You should see the Icecast status page.

## 3. Test Local Music Streaming

Create a playlist and verify ezstream can broadcast audio.

## 4. Test Kokoro TTS

Generate a short station ID and export audio.

## 5. Verify Stream URL

Open:

```text
http://localhost:8000/witradio
```

## 6. Test Radio Player

Open:

```text
web/radio-player.html
```

## 7. Test AI Music Generation

Generate one track for each show:

- Morning Acoustic
- Day Shift
- Dance Party
- Night Shift

## 8. Run WRIT Runtime

```bash
./writ start all
```

## 9. Verify Schedule Changes

Confirm the active show changes correctly by time of day.

## 10. Optional Future Enhancements

- YouTube livestream relay
- Cloudflare tunnel
- Public web player
- Listener messaging
- AI-generated commercials
- Monster-themed station IDs
