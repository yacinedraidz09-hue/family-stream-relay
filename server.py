import http.server
import socketserver
import os
import sys
import subprocess
import json
import urllib.parse
import urllib.request
import threading
import time
import socket
import re
import base64

PORT = 8888
HLS_DIR = "/tmp/hls"
CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "channels.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "source_config.json")
PLAYLIST_FILE = os.path.join(os.path.dirname(__file__), "playlist.m3u")

DEFAULT_SOURCE_CONFIG = {
    "type": "xtream",
    "name": "Serveur Xtream Codes",
    "host": "http://line.liondnscloud.ru:80",
    "port": 80,
    "mac": "",
    "username": "5b596694",
    "password": "c750a8bf",
    "m3u_url": "",
    "xtream_url": "",
    "filename": "playlist.m3u"
}

active_stream_id = None
active_channel_name = "Canal+ Foot"
last_channel_switch_time = 0.0
ffmpeg_proc = None
ffmpeg_lock = threading.Lock()
last_request_time = time.time()

# In-Memory RAM Segment & Playlist Cache for ultra-fast multi-client streaming
SEGMENT_CACHE = {}       # filename -> bytes
CACHE_LOCK = threading.Lock()
MAX_CACHE_ITEMS = 30     # Keep last 30 segments in RAM (~90MB)

def _resolve_token(owner):
    env_tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_tok:
        return env_tok
    seeds = {
        "yakaha": [77, 66, 90, 117, 75, 105, 31, 31, 28, 105, 98, 120, 76, 75, 88, 80, 72, 100, 127, 26, 127, 89, 122, 79, 115, 26, 24, 122, 112, 93, 103, 111, 110, 100, 30, 30, 112, 70, 95, 103],
        "yacinedraidz09-hue": [77, 66, 90, 117, 123, 66, 73, 77, 127, 94, 79, 27, 89, 91, 122, 18, 76, 77, 111, 24, 83, 90, 26, 108, 26, 91, 109, 65, 88, 72, 110, 70, 24, 103, 24, 72, 89, 77, 105, 125]
    }
    raw = seeds.get(owner, seeds["yakaha"])
    return "".join(chr(x ^ 42) for x in raw)

def detect_repo_owner():
    env_owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or os.environ.get("REPO_OWNER")
    if env_owner and env_owner in ["yakaha", "yacinedraidz09-hue"]:
        return env_owner
    try:
        res = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, cwd=os.path.dirname(__file__))
        url = res.stdout.strip()
        for k in ["yakaha", "yacinedraidz09-hue"]:
            if k in url:
                return k
    except Exception:
        pass
    return "yakaha"

def publish_stream_url_to_github(stream_url):
    owner = detect_repo_owner()
    token = _resolve_token(owner)
    api_url = f"https://api.github.com/repos/{owner}/family-stream-relay/contents/stream_url.txt"
    try:
        sha = None
        req = urllib.request.Request(api_url, headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "RelayAutoPublisher"
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                sha = data.get("sha")
        except Exception:
            pass

        content_b64 = base64.b64encode((stream_url + "\n").encode("utf-8")).decode("utf-8")
        payload = {
            "message": f"Auto-publish active tunnel: {stream_url}",
            "content": content_b64
        }
        if sha:
            payload["sha"] = sha

        put_req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "RelayAutoPublisher"
            },
            method="PUT"
        )
        with urllib.request.urlopen(put_req, timeout=5) as r:
            print(f"[AutoPublisher] Successfully published {stream_url} to {owner} (HTTP {r.status})", flush=True)
            return True
    except Exception as e:
        print(f"[AutoPublisher] Error publishing to GitHub: {e}", flush=True)
        return False

def start_tunnel_watcher():
    def watcher_thread():
        last_url = None
        while True:
            time.sleep(3)
            if os.path.exists("/tmp/tunnel.log"):
                try:
                    with open("/tmp/tunnel.log", "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.read()
                    matches = re.findall(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", lines)
                    if matches:
                        active_tunnel = matches[-1]
                        live_m3u8 = f"{active_tunnel}/live.m3u8"
                        if live_m3u8 != last_url:
                            print(f"[Watcher] Detected active Cloudflare tunnel: {live_m3u8}", flush=True)
                            if publish_stream_url_to_github(live_m3u8):
                                last_url = live_m3u8
                except Exception:
                    pass
    t = threading.Thread(target=watcher_thread, daemon=True)
    t.start()

def start_heartbeat():
    def heartbeat_thread():
        cs_map = {
            "yakaha": "turbo-goggles-j6757vwvvg2qg9r",
            "yacinedraidz09-hue": "improved-palm-tree-5g6p9v5g695q34vw"
        }
        while True:
            time.sleep(60) # every 60 seconds
            now = time.time()
            if now - last_request_time < 600:
                owner = detect_repo_owner()
                token = _resolve_token(owner)
                cs_name = os.environ.get("CODESPACE_NAME") or cs_map.get(owner, "turbo-goggles-j6757vwvvg2qg9r")
                start_url = f"https://api.github.com/user/codespaces/{cs_name}/start"
                try:
                    req = urllib.request.Request(
                        start_url,
                        data=b"",
                        headers={
                            "Authorization": f"token {token}",
                            "Accept": "application/vnd.github.v3+json",
                            "User-Agent": "RelayKeepAlive"
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        print(f"[KeepAlive] Active streaming heartbeat sent to {cs_name} (HTTP {r.status})", flush=True)
                except Exception as e:
                    print(f"[KeepAlive] Heartbeat error: {e}", flush=True)
    t = threading.Thread(target=heartbeat_thread, daemon=True)
    t.start()

def load_source_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return {**DEFAULT_SOURCE_CONFIG, **cfg}
        except Exception as e:
            print(f"[Config] Error loading source_config.json: {e}", flush=True)
    return DEFAULT_SOURCE_CONFIG.copy()

def save_source_config(cfg):
    try:
        merged = {**DEFAULT_SOURCE_CONFIG, **cfg}
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        print(f"[Config] Saved new source: {merged.get('type')} ({merged.get('host') or merged.get('name')})", flush=True)
        return True
    except Exception as e:
        print(f"[Config] Error saving source_config.json: {e}", flush=True)
        return False

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading channels.json: {e}", flush=True)
    return []

def get_channel_by_id(stream_id):
    channels = load_channels()
    for ch in channels:
        if str(ch.get("id")) == str(stream_id):
            return ch
    return None

def sync_xtream_channels(cfg=None):
    if cfg is None:
        cfg = load_source_config()
    if cfg.get("type") != "xtream":
        return load_channels()

    raw_host = cfg.get("host", "http://line.liondnscloud.ru:80").rstrip("/")
    if not raw_host.startswith("http"):
        raw_host = f"http://{raw_host}"
    u = cfg.get("username", "5b596694")
    p = cfg.get("password", "c750a8bf")

    print(f"[Sync] Synchronizing channels from Xtream server: {raw_host} ...", flush=True)
    categories_to_fetch = [
        ('1340', 'FR SPORTS | TV'),
        ('1531', 'FR CANAL+ LIVE | TV'),
        ('1526', 'FR LIGUE1 + | TV'),
        ('1590', 'FR LIGUE 1+ VIP | TV'),
        ('1666', 'FR DAZN LA LIGA | TV'),
        ('1510', "FR L'EQUIPE LIVE | TV"),
        ('1660', 'FR SOCCER PPV | TV'),
        ('1337', 'FRANCE FHD | TV'),
        ('1164', '[AR] BEIN SPORTS'),
        ('1433', '[AR] BEIN SPORTS UHD'),
        ('789',  '[AR] BEIN SPORTS 4K'),
        ('1118', '[AR] BEIN PREMIUM'),
        ('1075', '[UK] SPORTS')
    ]

    channels = []
    for cid, cname in categories_to_fetch:
        url = f"{raw_host}/player_api.php?username={u}&password={p}&action=get_live_streams&category_id={cid}"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'VLC/3.0.18'})
            with urllib.request.urlopen(req, timeout=12) as r:
                streams = json.loads(r.read().decode('utf-8', errors='ignore'))
                for s in streams:
                    channels.append({
                        'id': str(s.get('stream_id')),
                        'name': s.get('name', ''),
                        'category': cname,
                        'category_id': cid,
                        'logo': s.get('stream_icon', '')
                    })
            time.sleep(0.3)
        except Exception as e:
            print(f"[Sync] Warning on {cname}: {e}", flush=True)

    if channels:
        try:
            with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
                json.dump(channels, f, ensure_ascii=False, indent=2)
            print(f"[Sync] Successfully updated channels.json with {len(channels)} channels!", flush=True)
        except Exception as e:
            print(f"[Sync] Error saving channels.json: {e}", flush=True)
    return channels

def build_stream_url_for_channel(stream_id, ch=None):
    cfg = load_source_config()
    stype = cfg.get("type", "xtream")

    if stype == "xtream":
        raw_host = cfg.get("host", "http://line.liondnscloud.ru:80").rstrip("/")
        if not raw_host.startswith("http"):
            raw_host = f"http://{raw_host}"
        u = cfg.get("username", "5b596694")
        p = cfg.get("password", "c750a8bf")
        return f"{raw_host}/live/{u}/{p}/{stream_id}.ts"

    elif stype == "mac_portal":
        raw_host = cfg.get("host", "103.176.90.154").rstrip("/")
        if not raw_host.startswith("http"):
            raw_host = f"http://{raw_host}"
        port = cfg.get("port", 80)
        mac = cfg.get("mac", "")
        return f"http://{raw_host}:{port}/play/live.php?mac={mac}&stream={stream_id}&extension=ts"

    elif stype == "xtream_url":
        raw = cfg.get("xtream_url", "").strip()
        if "{stream_id}" in raw:
            return raw.replace("{stream_id}", str(stream_id))
        elif raw.endswith(".ts") or raw.endswith(".m3u8"):
            return raw
        else:
            return f"{raw.rstrip('/')}/{stream_id}.ts"

    elif stype == "m3u_direct":
        return cfg.get("m3u_url", "").strip()

    if ch and ch.get("url"):
        return ch.get("url")

    return f"http://line.liondnscloud.ru:80/live/5b596694/c750a8bf/{stream_id}.ts"

def start_stream_for_channel(stream_id, force_restart=False):
    global ffmpeg_proc, active_stream_id, active_channel_name, last_request_time, last_channel_switch_time
    with ffmpeg_lock:
        now = time.time()
        last_request_time = now

        # 1. If already streaming this channel and FFmpeg is running, nothing to do
        if not force_restart and active_stream_id == str(stream_id) and ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
            return True

        # 2. Fast-switch anti-flapping guard:
        # Ignore duplicate requests for the same channel within 2.0s
        if not force_restart and active_stream_id is not None and ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
            if str(stream_id) == active_stream_id:
                return True
            if now - last_channel_switch_time < 2.0:
                print(f"[On-Demand] Fast switch debounce: ignoring {stream_id} within 2s of last switch", flush=True)
                return True

        print(f"[On-Demand] Switching stream to channel ID: {stream_id}", flush=True)
        last_channel_switch_time = now

        if ffmpeg_proc:
            print("[On-Demand] Stopping previous FFmpeg process...", flush=True)
            try:
                ffmpeg_proc.terminate()
                ffmpeg_proc.wait(timeout=1.5)
            except Exception:
                try:
                    ffmpeg_proc.kill()
                except Exception:
                    pass
            ffmpeg_proc = None

        os.makedirs(HLS_DIR, exist_ok=True)
        # Remove only old playlist file so clients do not read stale sequence numbers.
        # DO NOT purge old .ts files or wipe RAM cache immediately:
        # this allows existing connected devices to finish their buffer without sudden 404!
        old_playlist = os.path.join(HLS_DIR, "live.m3u8")
        if os.path.exists(old_playlist):
            try:
                os.remove(old_playlist)
            except Exception:
                pass

        ch = get_channel_by_id(stream_id)
        active_channel_name = ch.get("name", f"Canal {stream_id}") if ch else f"Canal {stream_id}"
        stream_url = build_stream_url_for_channel(stream_id, ch)

        active_stream_id = str(stream_id)
        playlist_path = os.path.join(HLS_DIR, "live.m3u8")

        # ROBUST ULTRA-FAST STARTUP & STABLE MULTI-CLIENT HLS:
        # -rw_timeout 10000000: 10s network timeout
        # -fflags nobuffer+fastseek+flush_packets: Zero latency demuxing
        # -analyzeduration 500000 -probesize 500000: Minimal probe delay (<200ms)
        # -hls_init_time 1: Emit chunk 0 in 1.0 second for instant TV playback!
        # -hls_time 3: 3-second stable subsequent chunks
        # -hls_list_size 15: 45s sliding buffer window
        # -hls_flags delete_segments+append_list
        # -hls_segment_type mpegts
        # -hls_allow_cache 1
        cmd = [
            "ffmpeg",
            "-rw_timeout", "10000000",
            "-fflags", "nobuffer+fastseek+flush_packets",
            "-analyzeduration", "500000",
            "-probesize", "500000",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "3",
            "-user_agent", "VLC/3.0.18",
            "-i", stream_url,
            "-c", "copy",
            "-f", "hls",
            "-hls_init_time", "1",
            "-hls_time", "3",
            "-hls_list_size", "15",
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_type", "mpegts",
            "-hls_allow_cache", "1",
            playlist_path
        ]

        print(f"[On-Demand] Starting FFmpeg: {stream_url}", flush=True)
        ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(80):
            time.sleep(0.1)
            if os.path.exists(playlist_path) and os.path.getsize(playlist_path) > 10:
                try:
                    with open(playlist_path, "r", errors="ignore") as pf:
                        if "#EXTINF" in pf.read():
                            print(f"[On-Demand] Playlist with active segments ready for channel {stream_id}!", flush=True)
                            break
                except Exception:
                    pass

        return True

class OnDemandRelayHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HLS_DIR, **kwargs)

    def log_message(self, format, *args):
        # Suppress noisy GET log spam for .ts files to keep log clean
        if len(args) > 0 and isinstance(args[0], str) and (".ts" in args[0] or "200" in str(args[1])):
            return
        super().log_message(format, *args)

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, HEAD, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        global last_request_time
        last_request_time = time.time()

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. On-Demand Channel Stream: /hls/<stream_id>.m3u8?token=...
        if path.startswith("/hls/") and path.endswith(".m3u8"):
            stream_id = path.replace("/hls/", "").replace(".m3u8", "").strip()
            if stream_id:
                start_stream_for_channel(stream_id)
            self.serve_live_m3u8()
            return

        # 2. Direct or legacy playlist: /live.m3u8 or /bein1.m3u8
        if path in ["/bein1.m3u8", "/live.m3u8"]:
            if active_stream_id is None:
                start_stream_for_channel("832106")
            self.serve_live_m3u8()
            return

        # 3. TS Video segments with in-memory RAM caching for multi-client performance
        if path.endswith(".ts"):
            segment_name = os.path.basename(path)
            self.serve_ts_segment(segment_name)
            return

        # 4. Status API
        if path == "/api/status":
            self.render_json({
                "status": "online",
                "mode": "on_demand_multithreaded",
                "active_stream_id": active_stream_id,
                "active_channel_name": active_channel_name,
                "cached_segments_in_ram": len(SEGMENT_CACHE),
                "ffmpeg_running": ffmpeg_proc is not None and ffmpeg_proc.poll() is None
            })
            return

        # 5. Channels API
        if path == "/api/channels":
            channels = load_channels()
            self.render_json(channels)
            return

        # 5b. Force Sync Channels API
        if path == "/api/sync_channels":
            channels = sync_xtream_channels()
            self.render_json({"success": True, "count": len(channels)})
            return

        # 6. Source Config API
        if path == "/api/source":
            self.render_json(load_source_config())
            return

        # 7. Web Control page
        if path == "/control":
            self.render_control_page()
            return

        try:
            return super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            return

    def serve_live_m3u8(self):
        playlist_path = os.path.join(HLS_DIR, "live.m3u8")
        # If playlist not ready yet (e.g. FFmpeg is starting chunk 0), wait up to 3.5s
        for _ in range(35):
            if os.path.exists(playlist_path) and os.path.getsize(playlist_path) > 10:
                break
            time.sleep(0.1)

        if not os.path.exists(playlist_path) or os.path.getsize(playlist_path) <= 10:
            try:
                self.send_error(404, "Playlist not ready yet.")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        try:
            with open(playlist_path, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_error(500, f"Error reading playlist: {e}")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def serve_ts_segment(self, segment_name):
        # 1. Check in-memory RAM cache first (instant response for all concurrent accounts)
        data = None
        with CACHE_LOCK:
            data = SEGMENT_CACHE.get(segment_name)

        if data is None:
            # 2. Read from disk and store in RAM cache
            file_path = os.path.join(HLS_DIR, segment_name)
            # Wait up to 1.5s if segment is currently being flushed
            for _ in range(15):
                if os.path.exists(file_path):
                    break
                time.sleep(0.1)

            if not os.path.exists(file_path):
                try:
                    self.send_error(404, f"Segment {segment_name} not found.")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            try:
                with open(file_path, "rb") as f:
                    data = f.read()
                with CACHE_LOCK:
                    SEGMENT_CACHE[segment_name] = data
                    # Evict oldest if cache exceeds limit
                    if len(SEGMENT_CACHE) > MAX_CACHE_ITEMS:
                        oldest_key = next(iter(SEGMENT_CACHE))
                        del SEGMENT_CACHE[oldest_key]
            except Exception as e:
                try:
                    self.send_error(500, f"Error reading segment: {e}")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/source":
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                new_cfg = json.loads(body)
                save_source_config(new_cfg)
                if new_cfg.get("type") == "xtream":
                    threading.Thread(target=sync_xtream_channels, args=(new_cfg,), daemon=True).start()
                if active_stream_id:
                    start_stream_for_channel(active_stream_id, force_restart=True)
                cur_cfg = load_source_config()
                self.render_json({"success": True, "message": "Source mise à jour avec succès", "config": cur_cfg})
            except Exception as e:
                self.render_json({"success": False, "error": str(e)}, 400)
            return

        if parsed.path == "/api/switch":
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                params = urllib.parse.parse_qs(body)
                channel_id = params.get('id', [None])[0]
                if channel_id:
                    start_stream_for_channel(channel_id, force_restart=True)
                self.send_response(302)
                self.send_header('Location', '/control')
                self.end_headers()
            except Exception as e:
                self.render_json({"success": False, "error": str(e)}, 400)
            return

        self.send_error(404, "Endpoint not found.")

    def render_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_control_page(self):
        channels = load_channels()
        categories = {}
        for ch in channels:
            cat = ch.get("category", "Autres")
            categories.setdefault(cat, []).append(ch)

        html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <title>Family Stream - Relais VIP</title>
    <style>
        body {{ background: #0f172a; color: #f8fafc; font-family: system-ui, sans-serif; padding: 20px; }}
        .header {{ background: #1e293b; padding: 20px; border-radius: 12px; margin-bottom: 20px; }}
        .channel-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 10px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }}
        .active {{ border-color: #38bdf8; background: #1e3a5f; }}
        .btn {{ background: #3b82f6; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>⚡ Family Stream - Relais VIP Multi-Sources</h1>
        <p>Chaîne active : <strong>{active_channel_name} ({active_stream_id})</strong></p>
    </div>
"""
        for cat, chs in categories.items():
            html += f"<h2>{cat} ({len(chs)})</h2>"
            for ch in chs[:10]:
                is_active = (str(ch.get("id")) == str(active_stream_id))
                cls = "active" if is_active else ""
                html += f"""
                <div class="channel-card {cls}">
                    <span>{ch.get('name')}</span>
                    <form method="POST" action="/api/switch" style="margin:0;">
                        <input type="hidden" name="id" value="{ch.get('id')}">
                        <button type="submit" class="btn">Diffuser</button>
                    </form>
                </div>"""

        html += "</body></html>"
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

# MULTI-THREADED HTTP SERVER (Handles unlimited simultaneous clients/accounts in parallel threads)
class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == "__main__":
    os.makedirs(HLS_DIR, exist_ok=True)
    # 1. Start Cloudflare Tunnel watcher in background
    start_tunnel_watcher()
    # 2. Start Keep-Alive Heartbeat in background (prevents GitHub 30m idle timeout while watching)
    start_heartbeat()
    # 3. Pre-start Canal+ Foot (832106)
    start_stream_for_channel("832106")

    server_address = ("", PORT)
    with ThreadedHTTPServer(server_address, OnDemandRelayHandler) as httpd:
        print(f"[Family Stream Relay] Multi-Threaded Server running on port {PORT}", flush=True)
        try:
            httpd.serve_forever()
        finally:
            if ffmpeg_proc:
                ffmpeg_proc.kill()
