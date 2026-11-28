# Official Telegram Bot API server (Alpine/musl build); we copy the binary + its libs and run it via the musl loader.
FROM aiogram/telegram-bot-api:latest AS tba

FROM node:22-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/pot \
    && cd /opt/pot/server && npm ci && npx tsc -p tsconfig.json && npm prune --omit=dev
# Free Cloudflare WARP exit for YouTube only (best effort, see warp.sh)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/wgcf https://github.com/ViRb3/wgcf/releases/download/v2.3.1/wgcf_2.3.1_linux_amd64 \
    && chmod +x /usr/local/bin/wgcf \
    && curl -fsSL https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz | tar -xz -C /usr/local/bin wireproxy \
    && chmod +x /usr/local/bin/wireproxy
RUN mkdir -p /opt/tba/lib
COPY --from=tba /usr/local/bin/telegram-bot-api /opt/tba/telegram-bot-api
COPY --from=tba /lib/ld-musl-x86_64.so.1 /opt/tba/lib/
COPY --from=tba /usr/lib/libssl.so.3 /usr/lib/libcrypto.so.3 /usr/lib/libz.so.1 /usr/lib/libstdc++.so.6 /usr/lib/libgcc_s.so.1 /opt/tba/lib/
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY bot.py start.sh warp.sh ./
RUN chmod +x start.sh warp.sh
ENV HEALTH_PORT=7860
CMD ["./start.sh"]
