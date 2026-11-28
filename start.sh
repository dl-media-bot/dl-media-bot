#!/bin/sh
node /opt/pot/server/build/main.js &
# Best effort: never blocks or breaks the bot if WARP is unavailable.
./warp.sh >/tmp/warp.log 2>&1 &
sleep 2
exec python3 bot.py
