#!/bin/bash

# Build playlist based on current show

SHOW=$(python3 tools/current_show.py)

echo "Current show: $SHOW"

python3 tools/build_playlist.py "$SHOW"

cp "runtime/${SHOW}.m3u" runtime/current_playlist.m3u

echo "Playlist rotated successfully."
