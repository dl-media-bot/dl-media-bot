#!/bin/sh
# Best-effort Cloudflare WARP SOCKS proxy on 127.0.0.1:1080 (YouTube only).
# Registers a free account (wgcf), runs wireproxy, VERIFIES the tunnel really passes
# traffic (warp=on), rotates through Cloudflare endpoints/ports if the handshake
# does not complete, and re-checks every 60s. Never blocks the bot.
D=/tmp/warp; mkdir -p $D; cd $D || exit 0
ENDPOINTS="engage.cloudflareclient.com:2408 162.159.192.1:2408 162.159.192.1:500 162.159.192.1:4500 162.159.192.1:854 162.159.193.1:2408 162.159.193.1:500 [2606:4700:d0::a29f:c001]:2408"
probe() { curl -s -m 10 --socks5-hostname 127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -q 'warp=on'; }
register() {
  rm -f wgcf-account.toml wgcf-profile.conf wp.base
  wgcf register --accept-tos >/dev/null 2>&1 && wgcf generate >/dev/null 2>&1 && cp wgcf-profile.conf wp.base
}
start_wp() { # $1 = endpoint
  pkill wireproxy 2>/dev/null; sleep 1
  sed "s#^Endpoint = .*#Endpoint = $1#" wp.base > wp.conf
  printf '\n[Socks5]\nBindAddress = 127.0.0.1:1080\n' >> wp.conf
  wireproxy -c wp.conf >$D/wp.log 2>&1 &
}
fails=0
while true; do
  [ -s wp.base ] || { register || { echo "wgcf register failed, retry in 60s"; sleep 60; continue; }; }
  ok=0; rm -f $D/ok
  for ep in $ENDPOINTS; do
    start_wp "$ep"
    for t in 1 2 3; do sleep 5; if probe; then ok=1; break; fi; done
    if [ $ok = 1 ]; then echo "WARP tunnel verified on $ep"; touch $D/ok; break; fi
    echo "WARP endpoint $ep: no handshake, trying next"
  done
  if [ $ok = 1 ]; then
    fails=0
    while probe || { sleep 5; probe; }; do sleep 60; done
    rm -f $D/ok; echo "WARP tunnel stopped passing traffic, reconnecting"
  else
    fails=$((fails+1)); echo "WARP: no endpoint worked (round $fails), re-registering"
    rm -f wp.base; pkill wireproxy 2>/dev/null; sleep 30
  fi
done
