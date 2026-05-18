#!/bin/bash

# Quick local startup helper for WIT Radio

set -e

echo "Starting Icecast..."
icecast -c /opt/homebrew/etc/icecast.xml &

sleep 3

echo "Starting WRIT stream runtime..."
./writ start stream

sleep 2

echo "Opening local stream..."
echo "http://localhost:8000/witradio"
