#!/usr/bin/env bash
echo "=== Starting Family Stream Relay ==="
cd /workspaces/family-stream-relay 2>/dev/null || cd "$(dirname "$0")"

mkdir -p /tmp/hls
pkill -9 -f ffmpeg || true
pkill -9 -f cloudflared || true
pkill -9 -f server.py || true
pkill -9 -f "python3.*server.py" || true
rm -rf /tmp/hls/*
rm -f /tmp/tunnel.log
sleep 1

# 1. Start Python Server (with unbuffered stdout)
nohup python3 -u server.py > /tmp/http.log 2>&1 &
echo "Server started on port 8888"
sleep 1

# 2. Start Cloudflare Tunnel
nohup cloudflared tunnel --url http://127.0.0.1:8888 > /tmp/tunnel.log 2>&1 &
echo "Cloudflare Tunnel started"

# 3. Watchdog loop
while true; do
  sleep 15
  if ! pgrep -f "server.py" > /dev/null; then
    echo "[Watchdog] Restarting server.py..."
    nohup python3 -u server.py >> /tmp/http.log 2>&1 &
  fi
  if ! pgrep -f "cloudflared" > /dev/null; then
    echo "[Watchdog] Restarting cloudflared..."
    nohup cloudflared tunnel --url http://127.0.0.1:8888 >> /tmp/tunnel.log 2>&1 &
  fi
done
