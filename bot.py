import asyncio
import html as _html
import logging
import urllib.parse
import os
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("social-dl-bot")

# rolling download stats for the monitoring endpoint
import collections, time as _time
_dl_events = collections.deque(maxlen=500)  # (ts, ok:bool, platform:str)


def _record(ok: bool, platform: str) -> None:
    _dl_events.append((_time.time(), ok, platform))


def _stats() -> dict:
    now = _time.time()
    def window(sec):
        ev = [e for e in _dl_events if now - e[0] <= sec]
        return {"total": len(ev), "failures": sum(1 for e in ev if not e[1]),
                "fail_platforms": sorted({e[2] for e in ev if not e[1]})}
    return {"ok": True, "uptime_min": round((now - _START_TS) / 60, 1),
            "warp_up": warp_up(), "yt_warp_ok": _warp_stats[0], "yt_warp_fail": _warp_stats[1],
            "last_30m": window(1800), "last_6h": window(21600)}

_START_TS = _time.time()
_warp_stats = [0, 0]  # youtube downloads via WARP: [success, failed->fell back to direct]

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MAX_BYTES = 49 * 1024 * 1024  # stay under Telegram's 50MB bot-send cap

# Free Cloudflare WARP exit (userspace WireGuard SOCKS proxy started by warp.sh).
# Used ONLY for YouTube; every other platform keeps its direct path. If the proxy
# is down or the WARP attempt fails, the old direct path runs unchanged.
WARP_PROXY = "socks5://127.0.0.1:1080"
_warp_cache = [0.0, False]

URL_RE = re.compile(r"https?://[^\s]+", re.I)
SUPPORTED = re.compile(
    r"(tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com|instagram\.com|youtu\.be|"
    r"youtube\.com|twitter\.com|x\.com|facebook\.com|fb\.watch|fb\.com|"
    r"open\.spotify\.com|anghami\.com|reddit\.com|redd\.it)",
    re.I,
)
SPOTIFY_RE = re.compile(r"open\.spotify\.com", re.I)
ANGHAMI_RE = re.compile(r"anghami\.com", re.I)

MSG_START = (
    "أهلاً! ابعتلي لينك فيديو من تيك توك أو انستجرام أو يوتيوب أو إكس أو فيسبوك، "
    "وانا هنزلهولك وابعتهولك هنا على طول. وكمان لو بعتلي لينك أغنية من سبوتيفاي أو أنغامي هجيبهالك صوت.\n\n"
    "ملحوظة: تيليجرام بيسمح للبوتات تبعت فيديو لحد ٥٠ ميجا بس، فالفيديوهات الكبيرة جداً "
    "ممكن تتضغط أو ميقدرش يتبعت."
)
MSG_WORKING = "ثانية واحدة، بنزّل الفيديو... ⏳"
MSG_UNSUPPORTED = (
    "اللينك ده مش مدعوم. ابعت لينك من تيك توك أو انستجرام أو يوتيوب أو إكس أو فيسبوك."
)
MSG_NO_LINK = "مفيش لينك في الرسالة دي. ابعتلي لينك الفيديو وانا هتصرف."
MSG_FAIL = (
    "مقدرتش أنزّل من اللينك ده. ممكن يكون الفيديو برايفت أو المنصة حظرت التحميل دلوقتي. "
    "جرّب لينك تاني."
)
MSG_MUSIC_NOTE = (
    "🎵 ده مش من {src} نفسها: لقيت الأغنية بالاسم والمدة على يوتيوب ونزلتها منه، "
    "فالجودة ممكن تختلف وساعات يطلع إصدار تاني."
)
MSG_MUSIC_KIND = "أنا بدعم لينكات الأغاني الفردية بس (مش ألبوم أو بلاي ليست). ابعت لينك أغنية واحدة."
MSG_MUSIC_FAIL = (
    "معرفتش ألاقي الأغنية دي وأنزّلها. جرّب لينك أغنية تانية أو ابعتلي اسمها من يوتيوب."
)
MSG_LINK = (
    "الفيديو ده أكبر من ٥٠ ميجا (حد تيليجرام للبوتات) فمقدرتش أبعتهولك هنا. "
    "رفعته على رابط مؤقت تنزله منه (ممكن يتمسح بعد فترة، فنزله بسرعة):\n{url}"
)
MSG_TOO_BIG = (
    "للأسف الفيديو ده أكبر من ٥٠ ميجا حتى بعد ما ضغطته، وتيليجرام مش بيسمح للبوتات "
    "تبعت أكتر من كده. جرّب فيديو أقصر أو جودة أقل من المصدر."
)


TIKTOK_RE = re.compile(r"(tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com)", re.I)


def _save_mp4(resp_content: bytes, tmp: str) -> str | None:
    out = os.path.join(tmp, "video.mp4")
    with open(out, "wb") as f:
        f.write(resp_content)
    return out if os.path.getsize(out) > 10000 else None


def download_tiktok_tikwm(url: str, tmp: str) -> str | None:
    """Watermark-free TikTok download via the tikwm API (works on download-disabled videos)."""
    from curl_cffi import requests as creq
    try:
        api = "https://www.tikwm.com/api/?url=" + urllib.parse.quote(url, safe="") + "&hd=1"
        d = creq.get(api, impersonate="chrome", timeout=45).json()
        if d.get("code") != 0 or not d.get("data"):
            return None
        vurl = d["data"].get("hdplay") or d["data"].get("play")
        if not vurl:
            return None
        resp = creq.get(vurl, impersonate="chrome", timeout=180)
        if resp.status_code != 200 or not resp.content:
            return None
        return _save_mp4(resp.content, tmp)
    except Exception:
        log.exception("tikwm failed for %s", url)
        return None


def download_tiktok_ssstik(url: str, tmp: str) -> str | None:
    """Watermark-free TikTok download via ssstik.io (works on download-disabled videos)."""
    from curl_cffi import requests as creq
    try:
        s = creq.Session(impersonate="chrome")
        home = s.get("https://ssstik.io/", timeout=30)
        m = (re.search(r's_tt\s*=\s*["\']([^"\']+)', home.text)
             or re.search(r'tt\s*[:=]\s*["\']([^"\']+)', home.text))
        if not m:
            return None
        r = s.post(
            "https://ssstik.io/abc?url=dl",
            data={"id": url, "locale": "en", "tt": m.group(1)},
            headers={"Referer": "https://ssstik.io/", "HX-Request": "true"},
            timeout=60,
        )
        links = re.findall(r'href="(https?://[^"]+)"', r.text)
        vids = [l for l in links if "/ssstik/m/" not in l]
        if not vids:
            return None
        resp = s.get(vids[0], headers={"Referer": "https://ssstik.io/"}, timeout=180)
        if resp.status_code != 200 or not resp.content:
            return None
        return _save_mp4(resp.content, tmp)
    except Exception:
        log.exception("ssstik failed for %s", url)
        return None


TIKTOK_API_PATHS = (download_tiktok_ssstik,)

COBALT_HOSTS = ("https://rue-cobalt.xenon.zone", "https://cobaltapi.cjs.nz")
IG_RE = re.compile(r"instagram\.com", re.I)
YT_RE = re.compile(r"(youtu\.be|youtube\.com)", re.I)
RD_RE = re.compile(r"(reddit\.com|redd\.it)", re.I)


def download_cobalt(url: str, tmp: str) -> str | None:
    """Download via no-auth cobalt API instances (handles Instagram from datacenter IPs)."""
    from curl_cffi import requests as creq
    for host in COBALT_HOSTS:
        try:
            r = creq.post(
                host + "/",
                json={"url": url, "downloadMode": "auto", "videoQuality": "1080"},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                impersonate="chrome",
                timeout=30,
            )
            if r.status_code != 200:
                continue
            vurl = r.json().get("url")
            if not vurl:
                continue
            resp = creq.get(vurl, impersonate="chrome", timeout=180)
            ct = (resp.headers.get("content-type") or "").lower()
            if (resp.status_code == 200 and resp.content and len(resp.content) > 10000
                    and ("video" in ct or "octet-stream" in ct)):
                return _save_mp4(resp.content, tmp)
        except Exception:
            log.exception("cobalt %s failed for %s", host, url)
    return None




def _ff_hls(video_m3u8: str, audio_m3u8: str | None, out: str) -> bool:
    """Copy (no re-encode) Reddit's HLS video(+audio) into an mp4."""
    import subprocess
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", video_m3u8]
    if audio_m3u8:
        cmd += ["-i", audio_m3u8, "-map", "0:v", "-map", "1:a"]
    cmd += ["-c", "copy", "-movflags", "+faststart", out]
    try:
        subprocess.run(cmd, check=True, timeout=240, capture_output=True)
        return os.path.exists(out) and os.path.getsize(out) > 10000
    except Exception:
        log.warning("reddit ffmpeg hls failed for %s", video_m3u8)
        return False


def download_reddit(url: str, tmp: str) -> str | None:
    """Reddit blocks datacenter IPs (403 on reddit.com, also through WARP), but v.redd.it's HLS CDN and the
    vxreddit.com embed service answer. Resolve the post to its v.redd.it id via vxreddit, then copy the HLS streams."""
    from curl_cffi import requests as creq
    import urllib.parse as up
    vid = None
    m = re.search(r"v\.redd\.it/([a-z0-9]+)", url, re.I)
    if m:
        vid = m.group(1)
    else:
        try:
            p = up.urlparse(url)
            host = "vxreddit.com"
            r = creq.get(f"https://{host}{p.path}", headers={"User-Agent": "Twitterbot/1.0"}, timeout=25, allow_redirects=True)
            mm = re.search(r'og:video"\s+content="([^"]+)"', r.text)
            if mm:
                q = up.parse_qs(up.urlparse(mm.group(1).replace("&amp;", "&")).query)
                vm = re.search(r"v\.redd\.it/([a-z0-9]+)", (q.get("video_url") or [""])[0])
                vid = vm.group(1) if vm else None
        except Exception:
            log.exception("vxreddit lookup failed for %s", url)
    if not vid:
        return None
    base = f"https://v.redd.it/{vid}"
    audio = None
    for a in ("CMAF_AUDIO_128", "CMAF_AUDIO_64"):
        try:
            if creq.get(f"{base}/{a}.m3u8", timeout=15).status_code == 200:
                audio = f"{base}/{a}.m3u8"
                break
        except Exception:
            pass
    best = None
    for q in ("1080", "720", "480", "360", "240"):  # highest first; keep going down only while the file is too big
        out = os.path.join(tmp, f"video.mp4")
        if os.path.exists(out):
            os.remove(out)
        try:
            if creq.get(f"{base}/CMAF_{q}.m3u8", timeout=15).status_code != 200:
                continue
        except Exception:
            continue
        if _ff_hls(f"{base}/CMAF_{q}.m3u8", audio, out):
            best = out
            if os.path.getsize(out) <= MAX_BYTES:
                return out
    return best


def wait_warp(timeout: int = 100) -> bool:
    """Right after a deploy WARP needs up to ~1 min to come up; YouTube never works without it from a
    datacenter IP, so wait for it instead of falling through to a direct attempt that is sure to fail."""
    end = _time.time() + timeout
    while _time.time() < end:
        _warp_cache[0] = 0  # bypass the 15s cache
        if warp_up():
            return True
        _time.sleep(3)
    return warp_up()


def warp_up() -> bool:
    """True if the local WARP SOCKS port is open (cached 15s)."""
    import socket
    now = _time.time()
    if now - _warp_cache[0] < 15:
        return _warp_cache[1]
    if not os.path.exists("/tmp/warp/ok"):  # warp.sh creates it only after the tunnel passes real traffic
        _warp_cache[0], _warp_cache[1] = now, False
        return False
    try:
        with socket.create_connection(("127.0.0.1", 1080), timeout=1):
            ok = True
    except OSError:
        ok = False
    _warp_cache[0], _warp_cache[1] = now, ok
    return ok


def warp_probe(tag: str = "probe") -> None:
    """Log how fast the WARP tunnel really is (Cloudflare trace + YouTube homepage)."""
    from curl_cffi import requests as creq
    px = {"https": WARP_PROXY, "http": WARP_PROXY}
    for name, u in (("trace", "https://www.cloudflare.com/cdn-cgi/trace"), ("youtube", "https://www.youtube.com/generate_204")):
        t = _time.time()
        try:
            r = creq.get(u, proxies=px, timeout=20)
            log.info("WARP %s %s: HTTP %s in %.1fs", tag, name, r.status_code, _time.time() - t)
        except Exception as e:
            log.warning("WARP %s %s: FAILED after %.1fs: %s", tag, name, _time.time() - t, str(e)[:120])


YT_BUDGET = 46 * 1024 * 1024  # video+audio must fit under Telegram's cap without re-encoding


def pick_youtube_format(url: str, budget: int = None) -> tuple[str | None, bool]:
    """Via WARP: choose the best (<=1080p) video+audio pair whose estimated size fits the 50MB cap.
    Returns (format_string|None, known). format None with known=True means nothing fits (too long)."""
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "socket_timeout": 15,
         "retries": 1, "extractor_retries": 1, "proxy": WARP_PROXY}
    with yt_dlp.YoutubeDL(o) as ydl:
        info = ydl.extract_info(url, download=False)
    dur = info.get("duration") or 0
    budget = budget or YT_BUDGET

    def size(f):
        sz = f.get("filesize") or f.get("filesize_approx")
        if not sz and f.get("tbr") and dur:
            sz = f["tbr"] * 125 * dur
        return sz or 0

    fm = info.get("formats") or []
    vids = [f for f in fm if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
            and (f.get("height") or 0) <= 1080 and size(f)]
    auds = [f for f in fm if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none") and size(f)]
    if not vids or not auds:
        return None, False
    # smallest decent audio (m4a preferred, >= 48kbps)
    auds.sort(key=lambda f: (f.get("ext") != "m4a", size(f)))
    aud = auds[0]
    # best video that fits: higher resolution first, avc1 (plays everywhere) before others, then bitrate
    vids.sort(key=lambda f: (-(f.get("height") or 0), not str(f.get("vcodec", "")).startswith("avc1"), -size(f)))
    for v in vids:
        if size(v) + size(aud) <= budget:
            log.info("youtube pick: %sp %s ~%.1fMB (dur %ss)", v.get("height"), v.get("vcodec"),
                     (size(v) + size(aud)) / 1048576, dur)
            return f"{v['format_id']}+{aud['format_id']}", True
    return None, True


def ydl_opts_warp(outtmpl: str) -> dict:
    """YouTube via the WARP exit: default clients, no impersonation (as tested)."""
    return {
        "outtmpl": outtmpl,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/"
                  "b[height<=1080][ext=mp4]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True, "noprogress": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "proxy": WARP_PROXY,
    }


def ydl_opts(outtmpl: str) -> dict:
    return {
        "outtmpl": outtmpl,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/"
                  "b[height<=1080][ext=mp4]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True, "noprogress": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        "impersonate": ImpersonateTarget.from_str("chrome"),
        "extractor_args": {"youtube": {"player_client": ["mweb", "android", "web_embedded", "ios", "tv", "web"],
                                  "fetch_pot": ["always"]}},
    }


def compress(src: str, dst: str, scale: int, crf: int) -> bool:
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", f"scale=-2:{scale}",
        "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        dst,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=150)
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg compress timed out (scale %s)", scale)
        return False
    return r.returncode == 0 and os.path.exists(dst)


GOFILE_URL = "https://upload.gofile.io/uploadfile"
LINK_MAX_BYTES = 1250 * 1024 * 1024  # hard ceiling for the link fallback; the real limit also depends on free disk


def _sweep_tmp(max_age: int = 1800) -> None:
    """Delete leftover dl_/mu_ temp dirs (e.g. from timed-out jobs) so the free disk never fills."""
    base = tempfile.gettempdir()
    try:
        for n in os.listdir(base):
            p = os.path.join(base, n)
            if n[:3] in ("dl_", "mu_") and os.path.isdir(p) and _time.time() - os.path.getmtime(p) > max_age:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def yt_link_budget() -> int:
    """Largest YouTube video+audio (bytes) we try: parts plus merged file need ~2.2x on disk."""
    try:
        free = shutil.disk_usage(tempfile.gettempdir()).free
    except OSError:
        free = 0
    return int(max(0, min(1200 * 1024 * 1024, (free - 300 * 1024 * 1024) / 2.2)))


def upload_gofile(path: str) -> str | None:
    """Upload to Gofile as an anonymous guest (free, no account). Returns the download page or None."""
    from curl_cffi import requests as creq, CurlMime
    try:
        mp = CurlMime()
        mp.addpart(name="file", filename=os.path.basename(path), local_path=path, content_type="video/mp4")
        r = creq.post(GOFILE_URL, multipart=mp, timeout=600)
        d = r.json()
        if r.status_code == 200 and d.get("status") == "ok":
            return d["data"]["downloadPage"]
        log.warning("gofile upload failed: HTTP %s %s", r.status_code, str(d)[:150])
    except Exception:
        log.exception("gofile upload failed")
    return None


def link_fallback(path: str) -> tuple[None, str]:
    """File is over Telegram's cap: send a temporary download link instead."""
    if os.path.getsize(path) > LINK_MAX_BYTES:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        return None, "too_big"
    url = upload_gofile(path)
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    return (None, "link:" + url) if url else (None, "too_big")


def fit_or_link(path: str, tmp: str) -> tuple[str | None, str | None]:
    """Return (path, None) if it fits Telegram; try the compress ladder for modest overshoot, else a link."""
    if os.path.getsize(path) <= MAX_BYTES:
        return path, None
    if os.path.getsize(path) <= 150 * 1024 * 1024:
        for scale, crf in ((480, 28), (360, 32), (240, 35)):
            dst = os.path.join(tmp, f"compressed_{scale}.mp4")
            if compress(path, dst, scale, crf) and os.path.getsize(dst) <= MAX_BYTES:
                return dst, None
    return link_fallback(path)


def download_and_prepare(url: str) -> tuple[str | None, str | None]:
    """Return (path, None) on success or (None, error_code)."""
    _sweep_tmp()
    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        if TIKTOK_RE.search(url):
            got = None
            for api_fn in TIKTOK_API_PATHS:
                got = api_fn(url, tmp)
                if got:
                    break
            if got:
                return fit_or_link(got, tmp)
        if RD_RE.search(url):
            got = download_reddit(url, tmp)
            if got:
                return fit_or_link(got, tmp)
            shutil.rmtree(tmp, ignore_errors=True)
            return None, "fail"
        if IG_RE.search(url):
            got = download_cobalt(url, tmp)
            if got:
                return fit_or_link(got, tmp)
        outtmpl = os.path.join(tmp, "video.%(ext)s")
        done = False
        if YT_RE.search(url) and (warp_up() or wait_warp()):
            _t0 = _time.time()
            try:
                fmt, known = pick_youtube_format(url)
                if known and not fmt:
                    log.info("youtube video over the 50MB cap, trying link fallback: %s", url)
                    fmt, known = pick_youtube_format(url, yt_link_budget() or 1)
                    if known and not fmt:
                        shutil.rmtree(tmp, ignore_errors=True)
                        return None, "too_big"
                wopts = ydl_opts_warp(outtmpl)
                if fmt:
                    wopts["format"] = fmt
                with yt_dlp.YoutubeDL(wopts) as ydl:
                    ydl.download([url])
                done = any(f.startswith("video.") for f in os.listdir(tmp))
            except Exception:
                log.exception("youtube via WARP failed, falling back to direct: %s", url)
            log.info("youtube via WARP %s in %.1fs", "ok" if done else "FAILED", _time.time() - _t0)
            if not done:
                threading.Thread(target=warp_probe, args=("after-fail",), daemon=True).start()
            _warp_stats[0 if done else 1] += 1
            if not done:
                for f in os.listdir(tmp):
                    try:
                        os.remove(os.path.join(tmp, f))
                    except OSError:
                        pass
        if not done:
            with yt_dlp.YoutubeDL(ydl_opts(outtmpl)) as ydl:
                ydl.download([url])
        files = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("video.")]
        if not files:
            return None, "fail"
        path = max(files, key=os.path.getsize)
        return fit_or_link(path, tmp)
    except Exception:
        log.exception("download failed for %s", url)
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "fail"


def _meta(html: str, key: str) -> str | None:
    m = (re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(key), html)
         or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % re.escape(key), html))
    return _html.unescape(m.group(1)).strip() if m else None


def music_info(url: str) -> tuple[dict | None, str | None]:
    """Return ({title, artist, duration}, None) or (None, 'kind'|'fail') for a Spotify/Anghami track link."""
    from curl_cffi import requests as creq
    try:
        if SPOTIFY_RE.search(url):
            m = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)", url)
            if not m:
                return None, "fail"
            if m.group(1) != "track":
                return None, "kind"
            t = creq.get(f"https://open.spotify.com/track/{m.group(2)}", headers={"User-Agent": "Twitterbot/1.0"}, timeout=30).text
            title = _meta(t, "og:title")
            desc = _meta(t, "og:description") or ""
            artist = _meta(t, "music:musician_description") or (desc.split("\u00b7")[0].strip() if desc else "")
            dur = _meta(t, "music:duration")
            if not title:
                return None, "fail"
            return {"title": title, "artist": artist, "duration": int(dur) if dur and dur.isdigit() else None}, None
        # Anghami: the app page is behind a bot challenge, the lyrics page for the same song id is not.
        m = re.search(r"anghami\.com/(song|album|playlist|artist)/(\d+)", url)
        if not m:
            return None, "fail"
        if m.group(1) != "song":
            return None, "kind"
        t = creq.get(f"https://kalimat.anghami.com/lyrics/{m.group(2)}", impersonate="chrome", timeout=30).text
        page_title = _html.unescape((re.findall(r"<title>([^<]*)", t) or [""])[0]).replace("\u200f", "").strip()
        title, artist = None, ""
        mm = re.match(r"(.+) - (.+)$", page_title)
        if mm and "Lyrics" not in page_title and "\u0643\u0644\u0645\u0627\u062a" not in page_title:
            title, artist = mm.group(1).strip(), mm.group(2).strip()
        else:
            og = _meta(t, "og:title")
            mt = re.search(r"Lyrics to (.+) by (.+?)\s*$", _meta(t, "og:description") or "")
            if mt:
                title, artist = mt.group(1).strip(), mt.group(2).strip()
            elif og:
                title = og
        dur = _meta(t, "music:duration")
        if not title:
            return None, "fail"
        return {"title": title, "artist": artist, "duration": int(dur) if dur and dur.isdigit() else None}, None
    except Exception:
        log.exception("music_info failed for %s", url)
        return None, "fail"


def _norm_words(x: str) -> set:
    return set(re.findall(r"\w+", x.lower()))


def download_music(url: str) -> tuple[str | None, dict | None, str | None]:
    """Spotify/Anghami track -> best matching YouTube audio (m4a). Returns (path, info, err)."""
    info, err = music_info(url)
    if not info:
        return None, None, err
    tmp = tempfile.mkdtemp(prefix="mu_")
    try:
        q = f"{info['artist']} {info['title']}".strip()
        base = {"quiet": True, "noprogress": True, "noprogress": True, "no_warnings": True, "socket_timeout": 30, "retries": 3, "noplaylist": True}
        proxies = [WARP_PROXY] if (warp_up() or wait_warp()) else []
        proxies.append(None)  # direct as last resort
        want = _norm_words(info["title"]) | _norm_words(info["artist"])
        for proxy in proxies:
            try:
                o = dict(base, extract_flat=True)
                if proxy:
                    o["proxy"] = proxy
                with yt_dlp.YoutubeDL(o) as ydl:
                    res = ydl.extract_info(f"ytsearch8:{q} audio", download=False)
                best, best_score = None, -1e9
                for e in (res or {}).get("entries", []) or []:
                    if not e or not e.get("id"):
                        continue
                    words = _norm_words((e.get("title") or "") + " " + (e.get("channel") or e.get("uploader") or ""))
                    score = 3.0 * len(want & words) / max(len(want), 1)
                    d = e.get("duration")
                    if info["duration"] and d:
                        diff = abs(d - info["duration"])
                        if diff > 8:
                            continue
                        score += 2.0 - diff / 4.0
                    elif info["duration"]:
                        continue
                    low = (e.get("title") or "").lower()
                    if any(w in low for w in ("live", "cover", "remix", "karaoke", "lyrics video", "slowed", "sped")) \
                            and not any(w in info["title"].lower() for w in ("live", "cover", "remix")):
                        score -= 1.5
                    if "topic" in (e.get("channel") or e.get("uploader") or "").lower() or "official" in low:
                        score += 0.5
                    if score > best_score:
                        best, best_score = e, score
                if not best or best_score < 1.5:
                    continue
                o2 = dict(base, outtmpl=os.path.join(tmp, "audio.%(ext)s"),
                          format="bestaudio[ext=m4a]/bestaudio/best")
                if proxy:
                    o2["proxy"] = proxy
                with yt_dlp.YoutubeDL(o2) as ydl:
                    ydl.download(["https://www.youtube.com/watch?v=" + best["id"]])
                files = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("audio.")]
                if not files:
                    continue
                path = max(files, key=os.path.getsize)
                if not path.endswith(".m4a"):
                    dst = os.path.join(tmp, "audio_conv.m4a")
                    r = subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", "-c:a", "aac", "-b:a", "128k", dst], capture_output=True)
                    if r.returncode == 0 and os.path.exists(dst):
                        path = dst
                if os.path.getsize(path) > MAX_BYTES:
                    dst = os.path.join(tmp, "audio_small.m4a")
                    subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", "-c:a", "aac", "-b:a", "96k", dst], capture_output=True)
                    if os.path.exists(dst) and os.path.getsize(dst) <= MAX_BYTES:
                        path = dst
                    else:
                        return None, info, "too_big"
                return path, info, None
            except Exception:
                log.exception("music via %s failed for %s", proxy or "direct", url)
        shutil.rmtree(tmp, ignore_errors=True)
        return None, info, "fail"
    except Exception:
        log.exception("download_music failed for %s", url)
        shutil.rmtree(tmp, ignore_errors=True)
        return None, info, "fail"


async def handle_music(update: Update, url: str) -> None:
    src = "سبوتيفاي" if SPOTIFY_RE.search(url) else "أنغامي"
    platform = "spotify" if SPOTIFY_RE.search(url) else "anghami"
    status = await update.message.reply_text(MSG_WORKING)
    await update.message.chat.send_action(ChatAction.UPLOAD_VOICE)
    path, info, err = await asyncio.to_thread(download_music, url)
    if err == "kind":
        await status.edit_text(MSG_MUSIC_KIND)
        return
    if err == "too_big":
        _record(False, platform)
        await status.edit_text(MSG_TOO_BIG)
        return
    if err or not path:
        _record(False, platform)
        await status.edit_text(MSG_MUSIC_FAIL)
        return
    _record(True, platform)
    try:
        with open(path, "rb") as f:
            await update.message.reply_audio(audio=f, title=info["title"], performer=info["artist"] or None,
                                             duration=info.get("duration"), caption=MSG_MUSIC_NOTE.format(src=src))
        await status.delete()
    except Exception:
        log.exception("music send failed")
        await status.edit_text(MSG_MUSIC_FAIL)
    finally:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    urls = URL_RE.findall(text)
    supported = next((u for u in urls if SUPPORTED.search(u)), None)
    if not urls:
        await update.message.reply_text(MSG_NO_LINK)
        return
    if not supported:
        await update.message.reply_text(MSG_UNSUPPORTED)
        return

    if SPOTIFY_RE.search(supported) or ANGHAMI_RE.search(supported):
        await handle_music(update, supported)
        return

    status = await update.message.reply_text(MSG_WORKING)
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    try:
        path, err = await asyncio.wait_for(asyncio.to_thread(download_and_prepare, supported), timeout=900)
    except asyncio.TimeoutError:
        log.warning("download timed out after 900s: %s", supported)
        path, err = None, "fail"
    platform = ("tiktok" if TIKTOK_RE.search(supported) else
                "instagram" if IG_RE.search(supported) else
                "youtube" if YT_RE.search(supported) else
                "reddit" if RD_RE.search(supported) else "other")
    if err and err.startswith("link:"):
        _record(True, platform)
        await status.edit_text(MSG_LINK.format(url=err[5:]))
        return
    if err == "too_big":
        _record(False, platform)
        await status.edit_text(MSG_TOO_BIG)
        return
    if err or not path:
        _record(False, platform)
        await status.edit_text(MSG_FAIL)
        return
    _record(True, platform)
    try:
        with open(path, "rb") as f:
            await update.message.reply_video(video=f, supports_streaming=True)
        await status.delete()
    except Exception:
        log.exception("send failed")
        await status.edit_text(MSG_FAIL)
    finally:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MSG_START)






def run_health_server() -> None:
    port = int(os.environ.get("PORT") or os.environ.get("HEALTH_PORT") or "0")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/bot-stats-8bad075aeeda65e5ec15a9ea05fd47a0"):
                import json as _json
                body = _json.dumps(_stats()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main() -> None:
    if os.environ.get("PORT") or os.environ.get("HEALTH_PORT"):
        threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    threading.Thread(target=lambda: (_time.sleep(45), warp_probe("startup")), daemon=True).start()
    log.info("bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
