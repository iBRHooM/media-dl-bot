# Use the official Playwright image (noble = Ubuntu 24.04, ships Python 3.12).
# This image ships with Chromium + all required system libs preinstalled,
# multi-arch (amd64 + arm64), and is maintained by Microsoft. Avoids fragile
# combinations of apt chromium + Playwright downloads.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# OCI image labels (recognized by GHCR / Docker Hub for source linking)
LABEL org.opencontainers.image.source="https://github.com/ibrhoom/media-dl-bot"
LABEL org.opencontainers.image.description="Self-hosted Telegram bot for media downloads"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# ffmpeg is needed by yt-dlp for stream merging.
# wget is needed to fetch the yt-dlp binary below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp nightly (more reliable for fast-moving sites like TikTok/IG)
RUN wget -qO /usr/local/bin/yt-dlp \
    https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp && \
    chmod +x /usr/local/bin/yt-dlp

# Copy project files and install via pyproject.toml
# (pyproject.toml is the single source of truth for version + dependencies)
COPY pyproject.toml README.md ./
COPY *.py ./
RUN pip install --no-cache-dir .

# Create runtime directories with correct perms, owned by the non-root user.
# pwuser ships with the Playwright base image. When ./downloads and ./logs
# are volume-mounted, the HOST directories must be owned by pwuser's uid
# (see README deploy notes) or the bot cannot write to them.
RUN mkdir -p /app/downloads /app/logs && \
    chown -R pwuser:pwuser /app/downloads /app/logs && \
    chmod 755 /app/downloads /app/logs

# Run as non-root. Chromium's sandbox stays enabled (no --no-sandbox);
# docker-compose loads seccomp_profile.json to permit the user-namespace
# syscalls the sandbox requires.
USER pwuser

CMD ["python", "-u", "main.py"]
