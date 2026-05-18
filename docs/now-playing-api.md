# WIT Radio Now Playing API

## Goal

Provide a lightweight API endpoint for:

- current song
- active show
- host
- listener count
- recently played tracks

## Example Endpoint

```text
GET /now-playing
```

## Example Response

```json
{
  "station": "WIT Radio",
  "show": "Dance Party",
  "host": "Nova",
  "track": "Midnight Neon Run",
  "artist": "AI Generated",
  "listeners": 42
}
```

## Suggested Usage

- website overlays
- mobile apps
- Discord bots
- Twitch/YouTube overlays
- car dashboards
- Apple CarPlay experiments

## Future Ideas

- websocket updates
- request queue API
- live voting
- AI-generated metadata artwork
