#!/usr/bin/env bash
echo "=== Starting On-Demand HLS Relay ==="
cd /workspaces/family-stream-relay 2>/dev/null || cd "$(dirname "$0")"

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

# 2. Start Cloudflare Tunnel
nohup cloudflared tunnel --url http://127.0.0.1:8888 > /tmp/tunnel.log 2>&1 &
echo "Cloudflare Tunnel started"

# 3. Wait for tunnel URL (break as soon as found!)
TUNNEL_URL=""
for i in {1..35}; do
  sleep 1
  TUNNEL_URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' /tmp/tunnel.log 2>/dev/null | head -1)
  if [ -n "$TUNNEL_URL" ]; then
    echo "Found tunnel: $TUNNEL_URL"
    break
  fi
done

echo "Active Tunnel: $TUNNEL_URL"

if [ -n "$TUNNEL_URL" ]; then
  STREAM_URL="$TUNNEL_URL/live.m3u8"
  echo "STREAM READY: $STREAM_URL"
  echo "$STREAM_URL" > stream_url.txt
  git config user.name "FamilyStreamBot"
  git config user.email "bot@familystream.local"
  git add stream_url.txt
  git commit -m "Live stream URL: $STREAM_URL" || true
  git push origin main || true
fi

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
