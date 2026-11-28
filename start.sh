#!/bin/sh
node /opt/pot/server/build/main.js &
# Best effort: never blocks or breaks the bot if WARP is unavailable.
./warp.sh 2>&1 | sed -u "s/^/[warp] /" &
# Optional local Telegram Bot API server (uploads up to 2GB). Only used if credentials are set AND it comes up.
if [ -n "$TELEGRAM_API_ID" ] && [ -n "$TELEGRAM_API_HASH" ]; then
  mkdir -p /tmp/tba-data
  /opt/tba/lib/ld-musl-x86_64.so.1 --library-path /opt/tba/lib /opt/tba/telegram-bot-api \
    --local --http-port=8081 --dir=/tmp/tba-data 2>&1 | sed -u "s/^/[tba] /" &
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if curl -s -m 2 http://127.0.0.1:8081/ >/dev/null 2>&1; then
      export USE_LOCAL_API=1; break
    fi
  done
fi
sleep 2
exec python3 bot.py
