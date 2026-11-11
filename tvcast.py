#!/usr/bin/env python3
"""tvcast: send an HLS (.m3u8) stream to a Chromecast and control it from the terminal.

Usage:
  tv cast [URL]        cast the stream (URL from the clipboard if omitted) and keep an
                       interactive prompt open: pause, play, seek 48, +30, -30, status, quit
  tv pause | play | stop | status
  tv seek 48           jump to minute 48 (also 48:30)
  tv +30 / tv -30      skip forward / back N seconds
  tv devices           list the Chromecasts on your network

Options:
  --device NAME        Chromecast to use (default: config, else the first one found)
  --quality 720p       rewrite the "<N>p" part of the URL to pick a rendition
  --referer URL        Referer/Origin the stream server expects
  --debug              log every request the Chromecast makes

Defaults can live in ~/.tvcast.json: {"device": "Living Room", "quality": "720p", "referer": "https://example.com/"}
"""
import json, os, re, socket, subprocess, sys, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
MAX_WIDTH = 1920  # what a regular (non-Ultra/Google TV 4K) Chromecast can decode


def take(args, name, default):
    if name in args:
        i = args.index(name); value = args[i + 1]; del args[i:i + 2]; return value
    return default


cfg_file = Path.home() / ".tvcast.json"
cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
args = sys.argv[1:]
DEVICE = take(args, "--device", cfg.get("device"))
QUALITY = take(args, "--quality", cfg.get("quality"))
REFERER = take(args, "--referer", cfg.get("referer"))
DEBUG = "--debug" in args
args = [a for a in args if a != "--debug"]
if not args or args[0] in ("-h", "--help", "help"):
    sys.exit(__doc__)

import pychromecast  # noqa: E402  imported late so "tv --help" is instant


def connect():
    if DEVICE:
        casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[DEVICE])
    else:
        casts, browser = pychromecast.get_chromecasts(timeout=5)
    if not casts:
        sys.exit(f"Chromecast '{DEVICE or 'any'}' not found. Is it on and on the same Wi-Fi?")
    cc = casts[0]; cc.wait()
    return cc, browser


def status_line(mc):
    mc.update_status(); time.sleep(1)
    t = int(mc.status.current_time or 0)
    return f"{mc.status.player_state} {t // 60}:{t % 60:02d}"


def control(mc, cmd, arg=None):
    """Run one control command. Returns False if the command is unknown."""
    cmd = cmd.lower()
    mc.update_status()
    try:
        mc.block_until_active(timeout=5)
    except Exception:
        pass
    if not mc.status.media_session_id and cmd != "status":
        print("Nothing of mine is playing on that TV. Start with: tv cast"); return True
    if cmd in ("pause", "p"):
        mc.pause()
    elif cmd in ("play", "resume"):
        mc.play()
    elif cmd == "stop":
        mc.stop()
    elif cmd == "seek" and arg:
        parts = arg.split(":")
        mc.seek(int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0))
    elif len(cmd) > 1 and cmd[0] in "+-" and cmd[1:].isdigit():
        mc.update_status(); time.sleep(0.5)
        mc.seek(max(0, (mc.status.current_time or 0) + int(cmd)))
    elif cmd != "status":
        return False
    time.sleep(1.5)
    print(status_line(mc))
    return True


def devices():
    casts, browser = pychromecast.get_chromecasts(timeout=5)
    for c in casts:
        print(f"{c.cast_info.friendly_name}  ({c.cast_info.host}, {c.cast_info.model_name})")
    browser.stop_discovery()


MEDIA_RECEIVER = "CC1AD845"  # the Chromecast's Default Media Receiver app


def watch(cc, mc, done, every=30, offline_limit=4):
    """Call done(reason) when the movie ends, another app takes over the TV, or the TV goes away."""
    seen_playing, offline = False, 0
    while True:
        time.sleep(every)
        try:
            connected = cc.socket_client.is_connected
        except Exception:
            connected = False
        if not connected:
            offline += 1
            if offline >= offline_limit:
                return done("Lost the Chromecast (TV off?).")
            continue
        offline = 0
        if seen_playing and cc.app_id != MEDIA_RECEIVER:
            return done("Something else is playing on the TV now.")
        try:
            mc.update_status(); time.sleep(1)
        except Exception:
            continue
        state = mc.status.player_state
        if state in ("PLAYING", "PAUSED", "BUFFERING"):
            seen_playing = True
        elif seen_playing and state in ("IDLE", "UNKNOWN"):
            return done(f"Playback ended ({(mc.status.idle_reason or 'stopped').lower()}).")


def fetch(url, headers):
    for attempt in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)


def pick_variant(url, headers):
    """If the URL is a master playlist, pick the best rendition the Chromecast can decode."""
    text = fetch(url, headers).read().decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" not in text:
        return url
    best, best_w = None, -1
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            w = int(m.group(1)) if m else 0
            if best_w < w <= MAX_WIDTH:
                best, best_w = urllib.parse.urljoin(url, lines[i + 1].strip()), w
    return best or url


def cast(url):
    url = url or subprocess.run(["powershell", "-NoProfile", "-c", "Get-Clipboard"] if sys.platform == "win32"
                                else ["sh", "-c", "pbpaste 2>/dev/null || xclip -o -selection clipboard"],
                                capture_output=True, text=True).stdout
    url = url.strip()
    if QUALITY:
        url = re.sub(r"(?<=[^0-9])\d{3,4}p(?=[^0-9a-z])", QUALITY, url, count=1)
    if not url.startswith("http"):
        sys.exit("No URL given and none in the clipboard. Copy the .m3u8 link first.")

    headers = {"User-Agent": UA}
    if REFERER:
        headers["Referer"] = REFERER
        headers["Origin"] = REFERER.rstrip("/")
    url = pick_variant(url, headers)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); my_ip = s.getsockname()[0]; s.close()
    base = f"http://{my_ip}:{PORT}/p?u="

    def proxied(u, ref):
        return base + urllib.parse.quote(urllib.parse.urljoin(ref, u), safe="")

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            if DEBUG:
                print(self.client_address[0], urllib.parse.unquote(self.path)[7:110], a[1] if len(a) > 1 else "")

        def cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")

        def do_OPTIONS(self):
            self.send_response(204); self.cors(); self.end_headers()

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "u" not in q:
                self.send_response(404); self.end_headers(); return
            src = q["u"][0]
            hdrs = dict(headers)
            if self.headers.get("Range"):
                hdrs["Range"] = self.headers["Range"]
            try:
                r = fetch(src, hdrs)
            except Exception as e:
                print("Upstream error", src[:80], e); self.send_response(502); self.cors(); self.end_headers(); return
            body = r.read()
            ctype = r.headers.get("Content-Type", "application/octet-stream")
            if ".m3u8" in src.split("?")[0] or body[:7] == b"#EXTM3U":
                out = []
                for line in body.decode("utf-8", "replace").splitlines():
                    if line and not line.startswith("#"):
                        line = proxied(line, src)
                    elif 'URI="' in line:
                        line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{proxied(m.group(1), src)}"', line)
                    out.append(line)
                body = ("\n".join(out) + "\n").encode()
                ctype = "application/vnd.apple.mpegurl"
            try:
                self.send_response(r.status); self.cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if r.headers.get("Content-Range"):
                    self.send_header("Content-Range", r.headers["Content-Range"])
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass  # the Chromecast dropped the request (normal when seeking)

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Proxy)
    except OSError:
        sys.exit("Another 'tv cast' is already running. Close it (type quit) and try again.")
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print(f"Looking for '{DEVICE or 'a Chromecast'}'...")
    cc, browser = connect()
    mc = cc.media_controller
    mc.play_media(base + urllib.parse.quote(url, safe=""), "application/x-mpegURL", title="tvcast",
                  stream_type="BUFFERED", media_info={"hlsSegmentFormat": "fmp4", "hlsVideoSegmentFormat": "fmp4"})
    mc.block_until_active(timeout=30)
    print(f"Casting to {cc.cast_info.friendly_name}. Keep this window open while you watch.")
    print("Commands: pause, play, seek 48, +30, -30, status, quit (stops playback)")

    def done(reason):  # runs on the watcher thread, so exit the whole process from here
        print(f"\n{reason} Closing tvcast.")
        srv.shutdown(); browser.stop_discovery()
        os._exit(0)
    threading.Thread(target=watch, args=(cc, mc, done), daemon=True).start()
    try:
        while True:
            line = input("tv> ").strip().split()
            if not line:
                continue
            if line[0] in ("quit", "exit", "q"):
                break
            if not control(mc, line[0], line[1] if len(line) > 1 else None):
                print("Unknown command. Try: pause, play, seek 48, +30, -30, status, quit")
    except EOFError:  # no terminal (running in the background): keep serving, control with "tv pause" etc.
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    mc.stop(); srv.shutdown(); browser.stop_discovery()


def main():
    cmd = args[0].lower()
    if cmd == "cast":
        cast(args[1] if len(args) > 1 else None)
    elif cmd == "devices":
        devices()
    else:
        cc, browser = connect()
        if not control(cc.media_controller, cmd, args[1] if len(args) > 1 else None):
            print(__doc__)
        browser.stop_discovery()


if __name__ == "__main__":
    main()
