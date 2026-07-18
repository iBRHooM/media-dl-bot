# Media Downloader Bot

[![Release](https://img.shields.io/github/v/release/ibrhoom/media-dl-bot?include_prereleases&sort=semver)](https://github.com/ibrhoom/media-dl-bot/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Image](https://img.shields.io/badge/ghcr.io-media--dl--bot-blue?logo=docker)](https://github.com/ibrhoom/media-dl-bot/pkgs/container/media-dl-bot)

A self-hosted Telegram bot that downloads media from YouTube, TikTok, X / Twitter, Facebook, Instagram, Twitch, and Snapchat stories.

Built on `python-telegram-bot` v22.7 + `yt-dlp` + `Playwright`. Runs against a self-hosted Telegram Bot API server for **2 GB upload limits** (vs. the 50 MB limit on the public API).

---

## Installation

### 1. Prerequisites

- Docker + Docker Compose installed on your server
- A Telegram account
- Three Telegram credentials (steps below): `BOT_TOKEN`, `API_ID`, `API_HASH`

#### Get `BOT_TOKEN` (from @BotFather)

1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to pick a name and username.
3. BotFather replies with a token formatted like `123456789:ABCdef...`. Save it.

#### Get `API_ID` and `API_HASH` (from [my.telegram.org](https://my.telegram.org))

These are **not** for the bot — they're for the self-hosted Telegram Bot API container, which is what raises the upload limit from 50 MB to 2 GB.

1. Go to [my.telegram.org](https://my.telegram.org) and log in with your phone number.
2. Click **API development tools**.
3. Fill the form:
   - **App title:** `Media DL Bot` (any name works)
   - **Short name:** `mediadlbot` (alphanumeric, 5–32 chars)
   - **URL:** leave blank
   - **Platform:** Desktop
   - **Description:** optional
4. Click **Create application**.
5. The next page shows `App api_id` (a number) and `App api_hash` (a hex string). Save both.

> **Treat these credentials like passwords.** They identify a Telegram client and shouldn't be shared or committed anywhere public.

#### Find your Telegram user ID (optional, for `ALLOWED_USERS`)

If you want to restrict the bot to specific users (recommended), you'll need your numeric Telegram user ID:

1. Open Telegram and message [@userinfobot](https://t.me/userinfobot).
2. It replies with your user ID (a number like `123456789`).

### 2. Create the project directory

```bash
mkdir media-dl-bot && cd media-dl-bot
```

### 3. Create `docker-compose.yaml`

```bash
nano docker-compose.yaml
```

Paste:

```yaml
services:
  telegram-bot-api:
    image: aiogram/telegram-bot-api:latest
    container_name: telegram-bot-api
    restart: unless-stopped
    environment:
      TELEGRAM_API_ID: ${API_ID}
      TELEGRAM_API_HASH: ${API_HASH}
    volumes:
      - ./telegram-bot-api-data:/var/lib/telegram-bot-api
    # Not published on the host: the bot reaches this service over the
    # internal compose network. For local debugging only, you can add
    # ports: ["127.0.0.1:8081:8081"].
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:8081/ 2>&1 | grep -q '404'"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    deploy:
      resources:
        limits:
          cpus: "1"
          memory: 1G

  bot:
    image: ghcr.io/ibrhoom/media-dl-bot:${IMAGE_TAG:-latest}
    container_name: media-dl-bot
    restart: unless-stopped
    depends_on:
      telegram-bot-api:
        condition: service_healthy
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
      # API_ID / API_HASH are only needed by the telegram-bot-api
      # container above, not the bot — don't inject them here.
      LOCAL_API_URL: http://telegram-bot-api:8081
      ALLOWED_USERS: ${ALLOWED_USERS}
      MAX_FILE_SIZE_MB: ${MAX_FILE_SIZE_MB:-1900}
      TZ: ${TZ:-Asia/Riyadh}
    volumes:
      - ./downloads:/app/downloads
      - ./logs:/app/logs
    tmpfs:
      - /tmp
    # Allows Chromium's sandbox to create user namespaces (the bot runs
    # as non-root with the sandbox enabled). File fetched in the next step.
    security_opt:
      - seccomp:./seccomp_profile.json
    deploy:
      resources:
        limits:
          cpus: "2"
          memory: 2G
```

The compose file references `seccomp_profile.json` (Playwright's official
seccomp profile — Docker's default syscall allowlist plus the user-namespace
calls Chromium's sandbox needs). Download it next to the compose file:

```bash
wget https://raw.githubusercontent.com/iBRHooM/media-dl-bot/main/seccomp_profile.json
```

### 4. Create `.env`

```bash
nano .env
```

Paste, then fill in your real values:

```env
# ─── Required ─────────────────────────────────────────────────────────────────

# Telegram bot token from @BotFather (https://t.me/BotFather).
# Format: <numeric_id>:<alphanumeric_hash>, e.g. 123456789:ABCdef...
BOT_TOKEN=your_bot_token_here

# Telegram API credentials from https://my.telegram.org (Apps section).
# These are needed by the self-hosted Bot API container, NOT by the bot itself.
# Without them the Bot API container won't start, so the bot can't connect.
API_ID=your_api_id_here
API_HASH=your_api_hash_here

# ─── Optional ─────────────────────────────────────────────────────────────────

# Comma-separated Telegram user IDs allowed to use the bot.
# Leave empty to allow everyone (NOT recommended — anyone who finds your bot
# username can use your server's bandwidth and storage).
# To find your Telegram user ID, message @userinfobot.
# Example: ALLOWED_USERS=123456789,987654321
ALLOWED_USERS=

# Max file size in MB the bot will try to upload to Telegram.
# Hard cap is 2000 MB (2 GB) — the limit of the self-hosted Bot API.
# Files larger than this are skipped with a warning to the user.
# Default: 1900 (leaves headroom under the 2 GB hard cap)
MAX_FILE_SIZE_MB=1900

# Pin the bot image to a specific version. If omitted, `latest` is pulled.
# Recommended: pin in production so updates are deliberate, not surprise breakages.
# Example: IMAGE_TAG=0.1.0
IMAGE_TAG=latest

# Timezone for displaying Snapchat story post timestamps in Telegram captions.
# Use any IANA timezone name (https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).
# Examples: Asia/Riyadh, Europe/London, America/New_York, UTC
# Default: Asia/Riyadh
TZ=Asia/Riyadh
```

### 5. Start the bot

The container runs as a non-root user (`pwuser`), so the mounted
`downloads/` and `logs/` directories must be writable by its uid —
typically **1001** in the Playwright base image:

```bash
mkdir -p downloads logs
sudo chown -R 1001:1001 downloads logs
docker compose up -d
```

If the bot logs permission errors on startup, confirm the uid with
`docker compose exec bot id` and re-run the `chown` with that value.

The image is pulled from GHCR automatically. First start takes ~30 s while the Telegram Bot API container initializes.

### 6. Check the logs

```bash
docker compose logs -f bot
```

You should see:

```
Using local Bot API: http://telegram-bot-api:8081
Bot started (media-dl-bot v0.1.9).
```

Send `/start` to your bot in Telegram.

---

## Configuration reference

Quick reference for the variables in `.env`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram bot token from [@BotFather](https://t.me/BotFather). |
| `API_ID` | ✅ | — | Telegram API ID from [my.telegram.org](https://my.telegram.org). |
| `API_HASH` | ✅ | — | Telegram API hash from [my.telegram.org](https://my.telegram.org). |
| `ALLOWED_USERS` | optional | *empty* | Comma-separated Telegram user IDs allowed to use the bot. Empty = open to everyone. |
| `MAX_FILE_SIZE_MB` | optional | `1900` | Max file size to upload. Hard cap 2000 MB. |
| `TZ` | optional | `Asia/Riyadh` | IANA timezone for Snapchat story post timestamps in captions. |
| `IMAGE_TAG` | optional | `latest` | Pin the bot image to a specific version (e.g. `0.1.0`). |

---

## Updating

```bash
docker compose up -d --pull always
```

`--pull always` forces a registry check, which matters when you're tracking the floating `latest` tag. If you've pinned `IMAGE_TAG` to a specific version in `.env`, just bump the value and run `docker compose up -d` — switching tags is enough on its own.

---

## Usage

| Action | Input |
|---|---|
| Download YouTube video | Paste URL → pick quality |
| Download TikTok (no watermark) | Paste URL |
| Download X / Twitter video | Paste URL → pick quality |
| Download Facebook video | Paste URL → pick quality |
| Download Instagram reel/post | Paste URL |
| Download Twitch clip / VOD | Paste URL → pick quality |
| Download Snapchat stories | `snapchat <username>` → preview grid → pick snap or "Download all" |

---

## Notes

- Snapchat works for **public profiles only**.
- The self-hosted Bot API container is what raises the upload limit from 50 MB to 2 GB.
- Files are downloaded temporarily and deleted after sending.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for build-from-source and release instructions.

## License

MIT — see [LICENSE](LICENSE).
