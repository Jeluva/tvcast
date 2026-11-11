# tvcast

Cast any HLS (`.m3u8`) stream to a Chromecast from the terminal, even when the site has no Cast button
and the stream server only answers requests that look like they come from its own page.

```
$ tv cast
Looking for 'Living Room'...
Casting to Living Room. Keep this window open while you watch.
tv> seek 48
PLAYING 48:00
tv> pause
PAUSED 48:03
```

## Why

I was watching a movie on a website with no Cast support, so Chrome fell back to **tab casting**: the laptop
decodes the video, re-encodes the whole tab in real time and streams that over Wi-Fi. On battery it stuttered
every few seconds.

The obvious fix (VLC's "Render to Chromecast") played for a while and then stalled for good. So I measured
instead of guessing:

| Suspect | Measurement | Verdict |
|---|---|---|
| The stream server | a 6 s segment downloads in ~0.8 s | fine |
| The laptop CPU (VLC) | 5% CPU, no transcoding | fine |
| Wi-Fi to the TV | 0-3% packet loss, 2 ms, link at 721 Mbps | fine |
| **The video itself** | `ffprobe` on the init segment: **2540x1080** H.264 | **there it is** |

The "1080p" rendition was an ultrawide 2540 px stream. A regular Chromecast decodes up to 1920 px wide.
VLC sent it as is and the device choked; the native receiver refused it outright. The 720p rendition is
1692x720, which fits.

## How it works

```
Chromecast ──GET──> laptop :8765 (tiny proxy) ──GET + Referer/UA──> stream server
```

1. The stream server returns `403` unless the request carries the site's `Referer` and a browser `User-Agent`.
   A Chromecast can't send custom headers, so it can't fetch the stream directly.
2. `tvcast` starts a small HTTP proxy on the laptop. It fetches the playlist with the right headers and
   rewrites every segment URL (including `#EXT-X-MAP` init segments) to point back at itself.
3. It tells the Chromecast's built-in **Default Media Receiver** to play the proxied playlist
   (`hlsSegmentFormat: fmp4`). The Chromecast does the decoding with its own player, like Netflix does.
   The laptop only passes bytes through: no re-encoding, no remuxing.
4. If you pass a master playlist, it picks the best rendition that is at most 1920 px wide.
5. It closes itself when the movie ends, when something else starts playing on the TV, or when the TV
   is unreachable for about two minutes (TV turned off), so nothing stays running in the background.
6. Control works from the same window (`tv>` prompt) or from any other terminal (`tv pause`), because
   every command just opens a new Cast connection to the device.

About 250 lines of Python, one dependency ([pychromecast](https://github.com/home-assistant-libs/pychromecast)).

## Install

```bash
pip install pychromecast
git clone https://github.com/Jeluva/tvcast
```

Add a `tv` shortcut. On Windows, a `tv.cmd` somewhere in your PATH:

```bat
@python "%USERPROFILE%\Documents\tvcast\tvcast.py" %*
```

On macOS/Linux: `alias tv="python3 ~/tvcast/tvcast.py"`.

Allow Python through the firewall on your home network the first time (the Chromecast has to reach port 8765).

## Usage

Get the `.m3u8` URL with a stream detector browser extension (for example *The Stream Detector*), copy it, then:

```bash
tv cast                 # uses the URL in the clipboard
tv cast "https://.../index.m3u8"
tv pause
tv play
tv seek 48              # minute 48, or 48:30
tv +30                  # skip 30 s forward, -30 to go back
tv status
tv devices              # list Chromecasts on the network
```

Options: `--device "Living Room"`, `--quality 720p` (rewrites the `1080p` part of the URL),
`--referer https://site/`, `--debug` (prints every request the Chromecast makes).

Defaults go in `~/.tvcast.json`:

```json
{"device": "Living Room", "quality": "720p", "referer": "https://example.com/"}
```

## Limits

- The laptop has to stay on and on the same network while you watch (it's the proxy).
- Signed stream URLs expire; copy a fresh one each time.
- Only HLS. Sites using DRM (Widevine) won't work, by design.

Use it with content you have the right to watch.

## License

MIT
