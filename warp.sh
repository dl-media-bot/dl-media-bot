#!/bin/sh
# Best-effort Cloudflare WARP SOCKS proxy on 127.0.0.1:1080 (YouTube only).
# Restarts itself if wireproxy exits. If registration fails it retries with backoff.
D=/tmp/warp; mkdir -p $D; cd $D || exit 0
delay=30
while true; do
  if [ ! -s wp.conf ]; then
    rm -f wgcf-account.toml wgcf-profile.conf
    if wgcf register --accept-tos >/dev/null 2>&1 && wgcf generate >/dev/null 2>&1; then
      { cat wgcf-profile.conf; printf '\n[Socks5]\nBindAddress = 127.0.0.1:1080\n'; } > wp.conf
    else
      echo "wgcf register failed, retry in ${delay}s"; rm -f wp.conf
      sleep $delay; [ $delay -lt 900 ] && delay=$((delay*2)); continue
    fi
  fi
  wireproxy -c wp.conf
  echo "wireproxy exited, restarting in 15s"; sleep 15
done
