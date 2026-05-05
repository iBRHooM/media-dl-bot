# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-05-05

### Fixed
- Bot crashed at import with `ModuleNotFoundError: No module named 'playwright'`. The Playwright base image bundles the module in a path the app's site-packages cannot import; restored `playwright==1.48.0` as an explicit runtime dependency.
- Telegram Bot API healthcheck always failed because `localhost` resolved to IPv6 (`::1`) but the binary only binds IPv4. Switched the healthcheck to `127.0.0.1` and use `grep '404'` to validate the server is responding (Bot API has no `/` endpoint, so 404 is the expected response). Added a 30-second `start_period` to absorb cold-start time.

## [0.1.0] - 2026-05-04

Initial beta release.

### Added
- URL-based downloads: YouTube (videos & Shorts), TikTok (no watermark), X / Twitter, Facebook, Instagram (posts & reels), Twitch (clips & VODs).
- Username-based Snapchat public-story scraping via `snapchat <username>`.
- Inline quality picker for YouTube, X, Facebook, and Twitch (top 5 resolutions + "best available" option).
- Self-hosted Telegram Bot API container for 2 GB upload limit.
- Optional allow-list via `ALLOWED_USERS` env var.
- Configurable max file size (`MAX_FILE_SIZE_MB`, hard cap 2000).
- Per-download unique filename prefix to avoid collisions on concurrent requests.

[Unreleased]: https://github.com/ibrhoom/media-dl-bot/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.1
[0.1.0]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.0
