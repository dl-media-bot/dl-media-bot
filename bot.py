import asyncio
import logging
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
        "extractor_args": {"youtube": {"player_client": ["android", "web_embedded", "ios", "tv", "web"]}},
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


def run_health_server() -> None:
    port = int(os.environ.get("PORT") or os.environ.get("HEALTH_PORT") or "0")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main() -> None:
    if os.environ.get("PORT") or os.environ.get("HEALTH_PORT"):
        threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
