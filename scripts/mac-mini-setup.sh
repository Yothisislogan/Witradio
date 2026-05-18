#!/bin/bash

# WIT Radio Mac Mini bootstrap setup

set -e

echo "Installing Homebrew dependencies..."
brew install ffmpeg icecast ezstream vorbis-tools tmux

echo "Installing uv package manager..."
curl -LsSf https://astral.sh/uv/install.sh | sh

echo "Installing Python dependencies..."
uv sync

echo "Installing Kokoro TTS dependencies..."
cd mac/kokoro
uv venv
uv pip install kokoro soundfile
cd ../..

echo "Setup complete."
echo "Next steps:"
echo "1. Configure Icecast passwords"
echo "2. Start Icecast"
echo "3. Test ezstream"
echo "4. Generate AI voice segments"
echo "5. Generate music or import music library"
