FROM node:22-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/pot \
    && cd /opt/pot/server && npm ci && npx tsc -p tsconfig.json && npm prune --omit=dev
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY bot.py start.sh ./
RUN chmod +x start.sh
ENV HEALTH_PORT=7860
CMD ["./start.sh"]
