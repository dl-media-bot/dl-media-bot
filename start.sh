#!/bin/sh
node /opt/pot/server/build/main.js &
sleep 2
exec python3 bot.py
