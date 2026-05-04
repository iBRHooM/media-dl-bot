# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ibrhoom/media-dl-bot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.0
