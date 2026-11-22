import http.server
import socketserver
import os
import sys
import subprocess
import json
import urllib.parse
import threading
import time
import socket

PORT = 8888
HLS_DIR = "/tmp/hls"
CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "channels.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "source_config.json")
PLAYLIST_FILE = os.path.join(os.path.dirname(__file__), "playlist.m3u")

DEFAULT_SOURCE_CONFIG = {
    "type": "mac_portal",
    "name": "Serveur Principal Stalker",
    "host": "dinofox.sbs",
    "port": 80,
    "mac": "00:1A:79:B5:0D:61",
    "username": "",
    "password": "",
    "m3u_url": "",
    "xtream_url": "",
    "filename": "playlist.m3u"
}

active_stream_id = None
active_channel_name = "Aucune (En attente)"
ffmpeg_proc = None
ffmpeg_lock = threading.Lock()
last_request_time = time.time()

def load_source_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return {**DEFAULT_SOURCE_CONFIG, **cfg}
        except Exception as e:
            print(f"[Config] Error loading source_config.json: {e}")
    return DEFAULT_SOURCE_CONFIG.copy()

def save_source_config(cfg):
    try:
        merged = {**DEFAULT_SOURCE_CONFIG, **cfg}
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        print(f"[Config] Saved new source: {merged.get('type')} ({merged.get('host') or merged.get('name')})")
        return True
    except Exception as e:
        print(f"[Config] Error saving source_config.json: {e}")
        return False

def check_source_connectivity(cfg):
    stype = cfg.get("type", "mac_portal")
    host = ""
    port = 80
    if stype == "m3u_file":
        fn = cfg.get("filename", "playlist.m3u")
        fp = os.path.join(os.path.dirname(__file__), fn)
        has_file = os.path.exists(fp) or os.path.exists(CHANNELS_FILE)
        return {"online": has_file, "host": fn, "port": 0, "info": "Fichier M3U local"}

    if stype in ["mac_portal", "xtream"]:
        raw = cfg.get("host", "").replace("http://", "").replace("https://", "")
        if ":" in raw:
            parts = raw.split(":")
            host = parts[0].split("/")[0]
            try: port = int(parts[1].split("/")[0])
            except: port = 80
        else:
            host = raw.split("/")[0]
            port = int(cfg.get("port", 80) or 80)
    elif stype in ["xtream_url", "m3u_direct"]:
        url = cfg.get("xtream_url") or cfg.get("m3u_url") or ""
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    
    if not host:
        return {"online": False, "error": "Aucun hôte configuré"}
    
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.5)
        res = s.connect_ex((host, port))
        s.close()
        return {"online": (res == 0), "host": host, "port": port}
    except Exception as e:
        return {"online": False, "host": host, "port": port, "error": str(e)}

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading channels.json: {e}")
    return []

def get_channel_by_id(stream_id):
    channels = load_channels()
    for ch in channels:
        if str(ch.get("id")) == str(stream_id):
            return ch
    return None

def build_stream_url_for_channel(stream_id, ch=None):
    cfg = load_source_config()
    stype = cfg.get("type", "mac_portal")

    if stype == "m3u_file":
        if ch and ch.get("url"):
            return ch.get("url")
        m3u_fp = os.path.join(os.path.dirname(__file__), cfg.get("filename", "playlist.m3u"))
        if os.path.exists(m3u_fp):
            try:
                with open(m3u_fp, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.read().splitlines()
                    for i, l in enumerate(lines):
                        if str(stream_id) in l and i + 1 < len(lines):
                            nxt = lines[i + 1].strip()
                            if nxt.startswith("http"):
                                return nxt
            except Exception as e:
                print(f"[M3U] Error reading file: {e}")

    elif stype == "mac_portal":
        raw_host = cfg.get("host", "dinofox.sbs").replace("http://", "").replace("https://", "").rstrip("/")
        port = cfg.get("port", 80) or 80
        mac = cfg.get("mac", "00:1A:79:B5:0D:61")
        return f"http://{raw_host}:{port}/play/live.php?mac={mac}&stream={stream_id}&extension=ts"

    elif stype == "xtream":
        raw_host = cfg.get("host", "").rstrip("/")
        if not raw_host.startswith("http"):
            raw_host = f"http://{raw_host}"
        u = cfg.get("username", "")
        p = cfg.get("password", "")
        return f"{raw_host}/live/{u}/{p}/{stream_id}.ts"

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
    return f"http://dinofox.sbs:80/play/live.php?mac=00:1A:79:B5:0D:61&stream={stream_id}&extension=ts"

def start_stream_for_channel(stream_id, force_restart=False):
    global ffmpeg_proc, active_stream_id, active_channel_name, last_request_time
    with ffmpeg_lock:
        last_request_time = time.time()
        
        if not force_restart and active_stream_id == str(stream_id) and ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
            return True

        print(f"[On-Demand] Switching stream to channel ID: {stream_id}")
        
        if ffmpeg_proc:
            print("[On-Demand] Stopping previous FFmpeg process...")
            try:
                ffmpeg_proc.terminate()
                ffmpeg_proc.wait(timeout=2)
            except Exception:
                try:
                    ffmpeg_proc.kill()
                except Exception:
                    pass
            ffmpeg_proc = None

        os.makedirs(HLS_DIR, exist_ok=True)
        for f in os.listdir(HLS_DIR):
            try:
                os.remove(os.path.join(HLS_DIR, f))
            except Exception:
                pass

        ch = get_channel_by_id(stream_id)
        active_channel_name = ch.get("name", f"Canal {stream_id}") if ch else f"Canal {stream_id}"
        stream_url = build_stream_url_for_channel(stream_id, ch)

        active_stream_id = str(stream_id)
        playlist_path = os.path.join(HLS_DIR, "live.m3u8")

        cmd = [
            "ffmpeg",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            "-user_agent", "VLC/3.0.18",
            "-i", stream_url,
            "-c", "copy",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "6",
            "-hls_flags", "delete_segments",
            "-hls_segment_type", "mpegts",
            playlist_path
        ]
        
        print(f"[On-Demand] Starting FFmpeg: {stream_url}")
        ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(35):
            time.sleep(0.1)
            if os.path.exists(playlist_path) and os.path.getsize(playlist_path) > 0:
                print(f"[On-Demand] Playlist ready for channel {stream_id}!")
                break

        return True

class OnDemandRelayHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HLS_DIR, **kwargs)

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

    def do_GET(self):
        global last_request_time
        last_request_time = time.time()

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/hls/") and path.endswith(".m3u8"):
            stream_id = path.replace("/hls/", "").replace(".m3u8", "").strip()
            if stream_id:
                start_stream_for_channel(stream_id)
                self.path = "/live.m3u8"
                return super().do_GET()

        if path in ["/bein1.m3u8", "/live.m3u8"]:
            if active_stream_id is None:
                start_stream_for_channel("1178453")
            self.path = "/live.m3u8"
            return super().do_GET()

        if path.endswith(".ts"):
            if path.startswith("/hls/"):
                self.path = "/" + os.path.basename(path)
            return super().do_GET()

        if path == "/api/status":
            cfg = load_source_config()
            self.render_json({
                "status": "online",
                "mode": "on_demand",
                "active_stream_id": active_stream_id,
                "active_channel_name": active_channel_name,
                "ffmpeg_running": ffmpeg_proc is not None and ffmpeg_proc.poll() is None,
                "source_config": cfg,
                "source_health": check_source_connectivity(cfg)
            })
            return

        if path == "/api/source":
            cfg = load_source_config()
            self.render_json({
                "config": cfg,
                "health": check_source_connectivity(cfg)
            })
            return

        if path == "/api/channels":
            self.render_json(load_channels())
            return

        if path == "/control":
            self.render_control_page()
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == "/api/source":
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                new_cfg = json.loads(body)
                save_source_config(new_cfg)
                
                if active_stream_id:
                    start_stream_for_channel(active_stream_id, force_restart=True)
                    
                cur_cfg = load_source_config()
                self.render_json({
                    "success": True,
                    "message": "Source mise à jour avec succès",
                    "config": cur_cfg,
                    "health": check_source_connectivity(cur_cfg)
                })
            except Exception as e:
                self.render_json({"success": False, "error": str(e)}, 400)
            return

        if parsed.path == "/api/upload_m3u":
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                data = json.loads(body)
                m3u_text = data.get("content", "")
                channels = data.get("channels", [])
                
                # Save playlist.m3u
                if m3u_text:
                    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
                        f.write(m3u_text)
                
                # Save channels.json if provided
                if channels:
                    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
                        json.dump(channels, f, indent=2, ensure_ascii=False)
                        
                cfg = load_source_config()
                cfg["type"] = "m3u_file"
                cfg["filename"] = "playlist.m3u"
                save_source_config(cfg)
                
                if active_stream_id:
                    start_stream_for_channel(active_stream_id, force_restart=True)
                    
                self.render_json({"success": True, "channels_count": len(channels)})
            except Exception as e:
                self.render_json({"success": False, "error": str(e)}, 400)
            return

        if parsed.path == "/api/test_source":
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                cand_cfg = json.loads(body)
                health = check_source_connectivity(cand_cfg)
                self.render_json({"health": health})
            except Exception as e:
                self.render_json({"error": str(e)}, 400)
            return

        if parsed.path == "/api/switch":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            params = urllib.parse.parse_qs(body)
            target_id = params.get("id", [""])[0]

            if target_id:
                start_stream_for_channel(target_id)
                self.send_response(303)
                self.send_header("Location", "/control")
                self.end_headers()
                return

            self.render_json({"error": "Missing id"}, 400)
            return

        self.send_error(404, "Endpoint not found")

    def render_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_control_page(self):
        channels = load_channels()
        categories = {}
        for c in channels:
            categories.setdefault(c.get("category", "Autres"), []).append(c)
        cfg = load_source_config()

        html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Family Stream - Relais On-Demand Multi-Sources</title>
    <style>
        body {{
            background: #0f172a; color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0; padding: 20px;
        }}
        .container {{ max-width: 760px; margin: 0 auto; }}
        .header {{
            background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 24px;
            border: 1px solid #334155;
        }}
        .badge {{
            display: inline-block; background: #22c55e; color: #052e16; font-size: 12px;
            font-weight: 700; padding: 4px 10px; border-radius: 9999px; text-transform: uppercase;
        }}
        .source-box {{
            background: rgba(59,130,246,0.1); border: 1px solid rgba(59,130,246,0.3);
            border-radius: 8px; padding: 10px 14px; margin-top: 12px; font-size: 13px;
        }}
        h2 {{ color: #e2e8f0; font-size: 18px; margin-top: 24px; border-bottom: 2px solid #334155; padding-bottom: 8px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; margin-top: 12px; }}
        .channel-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 12px; }}
        .channel-card.active {{ border-color: #38bdf8; background: #1e3a5f; }}
        .btn {{ background: #3b82f6; color: white; border: none; border-radius: 6px; padding: 8px 12px; cursor: pointer; width: 100%; }}
        .btn.active-btn {{ background: #22c55e; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <span class="badge">🟢 Relais Multi-Sources Actif</span>
            <h1 style="margin: 10px 0 5px 0; font-size: 22px;">⚡ Family Stream - Relais VIP</h1>
            <div class="source-box">
                <strong>Source en direct :</strong> {cfg.get('type').upper()} ({cfg.get('host') or cfg.get('name') or 'Configurée'})
            </div>
            <div style="margin-top: 14px;">
                <div style="font-size: 12px; color: #94a3b8;">CHAÎNE DEMANDÉE :</div>
                <div style="font-size: 18px; font-weight: 700; color: #f59e0b; margin-top: 2px;">{active_channel_name}</div>
            </div>
        </div>
"""
        for cat_name, cat_channels in categories.items():
            html += f"<h2>{cat_name}</h2><div class='grid'>"
            for ch in cat_channels:
                is_active = (str(ch.get("id")) == str(active_stream_id))
                active_cls = " active" if is_active else ""
                btn_cls = " active-btn" if is_active else ""
                btn_text = "✓ En cours de diffusion" if is_active else "Tester manuellement"
                html += f"""
                <div class="channel-card{active_cls}">
                    <div style="font-weight:600;font-size:14px;margin-bottom:10px;">{ch.get('name')}</div>
                    <form method="POST" action="/api/switch">
                        <input type="hidden" name="id" value="{ch.get('id')}">
                        <button type="submit" class="btn{btn_cls}">{btn_text}</button>
                    </form>
                </div>
                """
            html += "</div>"

        html += "</div></body></html>"
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    os.makedirs(HLS_DIR, exist_ok=True)
    start_stream_for_channel("1178453")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), OnDemandRelayHandler) as httpd:
        print(f"[On-Demand Multi-Source] Server running on port {PORT}", flush=True)
        try:
            httpd.serve_forever()
        finally:
            if ffmpeg_proc:
                ffmpeg_proc.kill()
