import asyncio
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

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MAX_BYTES = 49 * 1024 * 1024  # stay under Telegram's 50MB bot-send cap

URL_RE = re.compile(r"https?://[^\s]+", re.I)
SUPPORTED = re.compile(
    r"(tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com|instagram\.com|youtu\.be|"
    r"youtube\.com|twitter\.com|x\.com|facebook\.com|fb\.watch|fb\.com)",
    re.I,
)

MSG_START = (
    "أهلاً! ابعتلي لينك فيديو من تيك توك أو انستجرام أو يوتيوب أو إكس أو فيسبوك، "
    "وانا هنزلهولك وابعتهولك هنا على طول.\n\n"
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




def ydl_opts(outtmpl: str) -> dict:
    return {
        "outtmpl": outtmpl,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/"
                  "b[height<=1080][ext=mp4]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
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
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.exists(dst)


def download_and_prepare(url: str) -> tuple[str | None, str | None]:
    """Return (path, None) on success or (None, error_code)."""
    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        if TIKTOK_RE.search(url):
            got = None
            for api_fn in TIKTOK_API_PATHS:
                got = api_fn(url, tmp)
                if got:
                    break
            if got:
                path = got
                if os.path.getsize(path) > MAX_BYTES:
                    for scale, crf in ((480, 28), (360, 32), (240, 35)):
                        dst = os.path.join(tmp, f"compressed_{scale}.mp4")
                        if compress(path, dst, scale, crf) and os.path.getsize(dst) <= MAX_BYTES:
                            path = dst
                            break
                    else:
                        return None, "too_big"
                return path, None
        if IG_RE.search(url):
            got = download_cobalt(url, tmp)
            if got:
                path = got
                if os.path.getsize(path) > MAX_BYTES:
                    for scale, crf in ((480, 28), (360, 32), (240, 35)):
                        dst = os.path.join(tmp, f"compressed_{scale}.mp4")
                        if compress(path, dst, scale, crf) and os.path.getsize(dst) <= MAX_BYTES:
                            path = dst
                            break
                    else:
                        return None, "too_big"
                return path, None
        outtmpl = os.path.join(tmp, "video.%(ext)s")
        with yt_dlp.YoutubeDL(ydl_opts(outtmpl)) as ydl:
            ydl.download([url])
        files = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("video.")]
        if not files:
            return None, "fail"
        path = max(files, key=os.path.getsize)
        if os.path.getsize(path) > MAX_BYTES:
            for scale, crf in ((480, 28), (360, 32), (240, 35)):
                dst = os.path.join(tmp, f"compressed_{scale}.mp4")
                if compress(path, dst, scale, crf) and os.path.getsize(dst) <= MAX_BYTES:
                    path = dst
                    break
            else:
                return None, "too_big"
        return path, None
    except Exception:
        log.exception("download failed for %s", url)
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "fail"


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

    status = await update.message.reply_text(MSG_WORKING)
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    path, err = await asyncio.to_thread(download_and_prepare, supported)
    if err == "too_big":
        await status.edit_text(MSG_TOO_BIG)
        return
    if err or not path:
        await status.edit_text(MSG_FAIL)
        return
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






def _probe_video(path: str) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def run_speed_probe() -> dict:
    """Temporary: time full downloads from inside this host. Remove after validation."""
    import time as _t
    out = {}
    for name, u in (("tiktok", "https://vt.tiktok.com/ZSs1wLHww/"),
                    ("instagram", "https://www.instagram.com/reel/DfwFdExq99d/")):
        t0 = _t.time()
        p, err = download_and_prepare(u)
        dt = round(_t.time() - t0, 1)
        info = {"sec": dt, "ok": bool(p), "err": err,
                "bytes": os.path.getsize(p) if p else 0,
                "video": _probe_video(p) if p else ""}
        out[name] = info
        if p:
            shutil.rmtree(os.path.dirname(p), ignore_errors=True)
    return out

def run_health_server() -> None:
    port = int(os.environ.get("PORT") or os.environ.get("HEALTH_PORT") or "0")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/debug-speed"):
                import json as _json
                body = _json.dumps(run_speed_probe()).encode()
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
    log.info("bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
