# Use the official Playwright image (noble = Ubuntu 24.04, ships Python 3.12).
# This image ships with Chromium + all required system libs preinstalled,
# multi-arch (amd64 + arm64), and is maintained by Microsoft. Avoids fragile
# combinations of apt chromium + Playwright downloads.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# OCI image labels (recognized by GHCR / Docker Hub for source linking)
LABEL org.opencontainers.image.source="https://github.com/iBRHooM/media-dl-bot"
LABEL org.opencontainers.image.description="Self-hosted Telegram bot for media downloads"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# ffmpeg is needed by yt-dlp for stream merging.
# yt-dlp itself comes from pip (pinned in pyproject.toml) — downloader.py
# imports the yt_dlp module; the previously-fetched standalone binary was
# never invoked and has been removed (unpinned, unverified download).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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
