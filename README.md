# DL Media Bot (@DL_Media_bot)

Telegram bot that downloads videos from TikTok, Instagram, YouTube, X (Twitter) and
Facebook links and sends them back in chat. Replies in Egyptian Arabic.

## Running

- Hosting: Render free web service (`srv-dc0qmjks728c73b3ksh0`), Docker, from this repo.
- Start command: `./start.sh` (runs the bgutil PO-token server on 127.0.0.1:4416, then `python3 bot.py`).
- Keep-alive: UptimeRobot pings `https://dl-media-bot.onrender.com/` every 5 minutes so the
  free instance never sleeps. Health endpoint: any GET on `/` returns `ok`.

## Environment variables (Render dashboard)

- `TELEGRAM_BOT_TOKEN` — BotFather token (stored in the agent vault, entry "Telegram bot token").
- `PORT` — set by Render automatically; the health server binds it.

## Deploying changes (IMPORTANT)

Render does NOT auto-deploy this service. After every push to `main`:

1. Push with the classic PAT from the agent vault ("GitHub deploy token (agent)").
2. Render dashboard -> service -> Manual Deploy -> Deploy latest commit.
3. Wait for "Deploy live" in Events, then check `https://dl-media-bot.onrender.com/` returns `ok`.

## Download paths per platform (in order)

- TikTok: ssstik.io scrape (no watermark, HD, works on download-disabled videos) -> yt-dlp.
  tikwm API is intentionally NOT used: it is 403-blocked from Render's IPs (verified 2026-11-21).
- Instagram: cobalt API instances (rue-cobalt.xenon.zone, fallback cobaltapi.cjs.nz) -> yt-dlp.
- YouTube / X / Facebook: yt-dlp (chrome impersonation, best video+audio merged up to 1080p,
  m4a, mp4 output). YouTube additionally uses a bgutil PO-token server in the same container.

## Known platform limits (verified 2026-11-21)

- YouTube: blocks datacenter IPs at the player level ("Sign in to confirm you're not a bot")
  even with PO tokens. No free fix. Paid fix ready to enable: residential proxy
  (DataImpulse $1/GB pay-as-you-go) passed to yt-dlp via --proxy. User declined for now.
- Telegram hosted Bot API caps bot-sent files at 50MB. Larger videos are re-compressed
  (480p/360p/240p ladder) and if still over, the bot explains the limit. Fix ready:
  self-host telegram-bot-api with --local (2GB uploads); needs api_id/api_hash.

## Fragility watch list

- ssstik and the cobalt instances are third-party free services; if one dies, swap in a new
  instance/provider in `COBALT_HOSTS` / `download_tiktok_ssstik` and redeploy.
- Render free tier: 750 instance-hours/month (one instance 24/7 fits), 512MB RAM.
- If YouTube errors suddenly change, re-test the POT setup (`yt-dlp` + bgutil plugin versions
  are pinned in requirements.txt; bump both together).

## Repo layout

- `bot.py` — everything: message handling, provider chains, compression, health server.
- `start.sh` — launches PO-token server + bot.
- `Dockerfile` — node:22-bookworm + python3 + ffmpeg + bgutil server build.
- `requirements.txt` — pinned python deps (curl_cffi must stay 0.16.x for yt-dlp impersonation).
