# WIT Radio Oracle + Gemini Pivot

## Current Status

As of May 27, 2026, the Oracle Cloud server is the public broadcast tower.

Working pieces:

- Ubuntu server on Oracle Cloud
- Icecast2 installed and reachable
- public ports 8000 and 8001 open
- Witradio repo installed
- WRIT runtime available
- music-gen.server cloned and models downloaded

## Why We Are Pivoting

The Oracle Free Tier CPU server is not a good place to generate AI music.

ACE-Step and similar models need GPU acceleration. On CPU-only Oracle hardware, one track can take far too long and cause the rest of the station automation to wait.

That delay also creates an API-cost risk if the DJ/operator loop keeps resending large context while waiting for music.

## New Strategy

Use Oracle Cloud for broadcasting, not heavy generation.

Oracle should run:

- Icecast
- WIT Radio runtime
- playlists
- metadata API
- web player
- health checks

Generation should happen elsewhere:

- Gemini for DJ scripts
- Google AI Studio or Lyria for music generation if available
- RTX 2080 Ti PC for local GPU generation experiments
- Musicgen repo as the approved-audio lab

## Safe Broadcast Rule

The live station should only play finished audio files.

Do not make the stream wait for live AI music generation.

## Immediate Next Steps

1. Store the Gemini API key securely on the server.
2. Update the LLM configuration to Gemini.
3. Stop any Claude/operator loop that is burning tokens.
4. Upload approved songs into the server music folders.
5. Rebuild playlists.
6. Start WIT Radio in static or pre-generated music mode.
7. Confirm Icecast plays the stream publicly.

## Server Music Folders

Recommended folders:

- ~/Music/morning_acoustic
- ~/Music/day_shift
- ~/Music/dance_party
- ~/Music/night_shift

Or inside the repo:

- music/morning_acoustic
- music/day_shift
- music/dance_party
- music/night_shift

## Security Note

Never paste API keys into chat, GitHub, public config files, or logs.

Use environment variables or private server-only config files.
