#!/usr/bin/env bash
echo "=== Starting On-Demand HLS Relay ==="
cd /workspaces/family-stream-relay 2>/dev/null || cd "$(dirname "$0")"

# Pull latest repo code
git config --global --add safe.directory /workspaces/family-stream-relay 2>/dev/null || true
git fetch origin main 2>/dev/null || true
git reset --hard origin/main 2>/dev/null || true

mkdir -p /tmp/hls
pkill -9 -f ffmpeg || true
pkill -9 -f cloudflared || true
pkill -9 -f server.py || true
pkill -9 -f "python3.*server.py" || true
rm -rf /tmp/hls/*
sleep 1

# 1. Start Python On-Demand Server & Stream Manager
nohup python3 server.py > /tmp/http.log 2>&1 &
echo "On-Demand Server running on port 8888"
sleep 1

# 2. Determine public URL (Codespaces app.github.dev)
if [ -n "$CODESPACE_NAME" ]; then
  STREAM_URL="https://${CODESPACE_NAME}-8888.app.github.dev/live.m3u8"
  echo "Codespace Public URL: $STREAM_URL"
  echo "$STREAM_URL" > stream_url.txt
fi

# 3. Start Cloudflare Tunnel as fallback
nohup cloudflared tunnel --url http://127.0.0.1:8888 > /tmp/tunnel.log 2>&1 &
echo "Cloudflare Tunnel started"

# 4. Watchdog keep-alive loop so Codespace container stays alive permanently
while true; do
  sleep 20
  if ! pgrep -f "server.py" > /dev/null; then
    echo "[Watchdog] Restarting server.py..."
    nohup python3 server.py >> /tmp/http.log 2>&1 &
  fi
  if ! pgrep -f "cloudflared" > /dev/null; then
    echo "[Watchdog] Restarting cloudflared..."
    nohup cloudflared tunnel --url http://127.0.0.1:8888 >> /tmp/tunnel.log 2>&1 &
  fi
done
