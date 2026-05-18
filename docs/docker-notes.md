# WIT Radio Docker Notes

## Goal

Containerize supporting services where practical.

## Good Docker Candidates

- Icecast
- API services
- now-playing endpoint
- listener messaging service
- lightweight dashboards
- ffmpeg relay jobs

## Services Better Run Natively

These may work better directly on Apple Silicon initially:

- Kokoro TTS
- ACE-Step music generation
- GPU-assisted AI generation
- realtime audio processing

## Suggested Future Compose Stack

```yaml
services:
  icecast:
  api:
  dashboard:
  cloudflare:
  youtube-relay:
```

## Recommendation

Start local and simple first.
Avoid over-containerizing before the stream is stable.
