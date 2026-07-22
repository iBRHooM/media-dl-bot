# Chromium-only image on ubuntu:24.04 (noble — the same userland as the
# retired Playwright base image, and ships Python 3.12 to match
# requires-python). Firefox and WebKit, which the Playwright base bundled
# but this bot never uses (snapchat.py uses chromium.launch only), are no
# longer installed — dropping them roughly halved the image.
FROM ubuntu:24.04

# OCI image labels (recognized by GHCR / Docker Hub for source linking)
LABEL org.opencontainers.image.source="https://github.com/iBRHooM/media-dl-bot"
LABEL org.opencontainers.image.description="Self-hosted Telegram bot for media downloads"
LABEL org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/opt/venv/bin:$PATH" \
    PIP_NO_CACHE_DIR=1

# System packages:
#  - python3 + python3-venv: runtime (noble = Python 3.12)
#  - ffmpeg: yt-dlp stream merge + FFmpegVideoRemuxer (downloader.py)
#  - fonts-dejavu-core: Pillow grid-number rendering (snapchat.py); without
#    it the numbers fall back to a tiny bitmap font
#  - ca-certificates: TLS for the Playwright browser download
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Isolated venv: avoids PEP 668 "externally managed environment" on noble.
RUN python3 -m venv /opt/venv

# Copy project files and install via pyproject.toml
# (pyproject.toml is the single source of truth for version + dependencies)
COPY pyproject.toml README.md ./
COPY *.py ./

# Install app deps (incl. playwright) and Chromium's OS-level deps (apt, so
# root). The browser BINARIES are deliberately NOT downloaded here — that
# happens as pwuser below, so ~1 GB of browser files are written already
# owned by pwuser and never need a recursive chown (a chown -R in a later
# layer would copy the whole tree up into a new layer — ~674 MB wasted).
RUN pip install . \
    && playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Non-root user. noble ships a default 'ubuntu' user at uid 1000, so pwuser
# is created at uid 1001 (matches the README deploy note). chown here only
# touches EMPTY dirs — the browser tree is populated later, as pwuser. When
# ./downloads and ./logs are volume-mounted, the HOST dirs must be owned by
# pwuser's uid (see README deploy notes) or the bot cannot write to them.
RUN useradd --create-home --uid 1001 pwuser \
    && mkdir -p /ms-playwright /app/downloads /app/logs \
    && chown pwuser:pwuser /ms-playwright /app/downloads /app/logs \
    && chmod 755 /app/downloads /app/logs

# Run as non-root with Chromium's sandbox enabled (no --no-sandbox);
# docker-compose loads seccomp_profile.json to permit the user-namespace
# syscalls the sandbox requires.
USER pwuser

# Download Chromium as pwuser → files land owned by pwuser (no chown, no
# copy-up). playwright picks the Chromium build matching the pinned version.
RUN playwright install chromium

CMD ["python3", "-u", "main.py"]
