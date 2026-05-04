# Contributing

## Build from source (Docker)

```bash
git clone https://github.com/ibrhoom/media-dl-bot.git
cd media-dl-bot
cp .env.example .env
nano .env
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d --build
```

This uses `docker-compose.dev.yaml` to build the image locally instead of pulling from GHCR. The build uses the official Playwright base image, which already ships Chromium and the Playwright Python package.

## Running outside Docker (bare metal)

Playwright is intentionally **not** listed in `pyproject.toml` dependencies because it's provided by the Docker base image. If you want to run the bot outside Docker for development or testing, install Playwright separately after `pip install .`:

```bash
pip install -e .
pip install playwright==1.48.0
playwright install chromium
```

You'll also need `ffmpeg` and `yt-dlp` available on your `$PATH`.

## Releasing a new version

1. Bump `version` in `pyproject.toml`.
2. Add an entry to `CHANGELOG.md`.
3. Commit and push.
4. Tag the release: `git tag v0.1.1 && git push --tags`.

The GitHub Actions workflow (`.github/workflows/release.yml`) automatically builds a multi-arch image (`linux/amd64`, `linux/arm64`) and pushes it to GHCR with tags `0.1.1`, `0.1`, `0`, and `latest`.
