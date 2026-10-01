FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg is required for merging/trimming/splitting; ca-certificates for TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rubika_dl ./rubika_dl
COPY scripts ./scripts

# Run as an unprivileged user.
RUN useradd -m -u 10001 bot && chown -R bot:bot /app
USER bot

ENV STATE_DIR=/app/.state \
    DOWNLOAD_DIR=/app/downloads \
    STATE_GIT_PUSH=false

CMD ["python", "-m", "rubika_dl", "run"]
