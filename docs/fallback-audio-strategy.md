# WIT Radio Fallback Audio Strategy

## Goal

Prevent dead air if:

- AI generation fails
- ffmpeg crashes
- APIs timeout
- music queues run empty
- network services disconnect

## Recommended Fallback Layers

### Layer 1

Looping emergency music playlist.

### Layer 2

Station IDs and bumpers.

### Layer 3

Pre-generated overnight content.

### Layer 4

Static backup stream.

## Recommended Fallback Content

- lo-fi instrumentals
- synthwave loops
- acoustic instrumentals
- station branding IDs
- generic music beds

## Operational Tip

Always keep at least:

- 3 hours of backup audio
- 1 hour of backup overnight programming
- multiple local playlist copies

## Future Enhancements

- automatic silence detection
- automatic stream switching
- Discord/Slack outage alerts
- auto-restart scripts
