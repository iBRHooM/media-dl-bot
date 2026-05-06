# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.5] - 2026-05-06

### Fixed
- **X / Twitter quote-tweets still failed** with `.NA` extension on v0.1.4. The HTTP-format variants Twitter ships report `vcodec=unknown`/`acodec=unknown`, which causes the `bestvideo*+bestaudio` selector to either skip them or mux them into a `.NA` container the muxer can't finalize. Rewrote the default format selector to prefer HLS-merged downloads (HLS streams have proper codec metadata and merge into mp4 cleanly) with progressive mp4 as a fallback for platforms like TikTok and Instagram. Also added `playlist_items='1'` to `fetch_formats` — without it, the format scan on quote-tweets returned a playlist dict (no top-level `formats` field), the bot saw "no qualities" and fell through to the auto-best path that hit the `.NA` bug.
- **File-resolution scan was extension-dependent.** When yt-dlp wrote a non-mp4 file (or left a `.NA` placeholder alongside the remuxer's `.mp4`), the resolver could either miss the real output or pick the wrong one. Resolution now scans the downloads directory by the per-download unique prefix and prefers `.mp4` when multiple matches exist — extension-agnostic, deterministic, and safe under concurrent downloads thanks to the unique prefix.

### Added
- **Global error handler** (`Application.add_error_handler`). Without this, ptb's default behaviour was to log unhandled exceptions under its own logger name at ERROR level with no traceback. v0.1.4 had a Snapchat handler exception that produced an error message in Telegram but left no usable trace in `docker compose logs bot`. The new handler logs every unhandled exception with full traceback under the `__main__` logger.
- **Detailed Snapchat scraping logs** (`fetch_snapchat_stories`). Each URL pattern attempt, HTTP status, `__NEXT_DATA__` size, and parse outcome now logs at INFO so we can see exactly where scraping bails out. Catastrophic Playwright failures are re-raised as `RuntimeError` so they surface to the user via the existing handler, instead of crashing into the global error handler with a generic message.
- **`handle_snapchat` outermost try/except** now wraps the initial `reply_text` (Markdown parsing of the username could raise) and the cleanup phase, eliminating any code path that can leak an exception unlogged.

## [0.1.4] - 2026-05-05

### Security
- **Bot token was being logged in plaintext** by `httpx` (the HTTP client used by python-telegram-bot). Every Telegram API call was producing an INFO log line with the full token in the URL, which then ended up in `bot.log`. Silenced the `httpx` and `httpcore` loggers down to WARNING level. Existing tokens that may have appeared in logs should be revoked via `/revoke` in @BotFather.

### Fixed
- **Twitter quote-tweet downloads failed** when both the outer tweet and the quoted tweet contained video. yt-dlp was returning both as a 2-entry "playlist" despite `noplaylist=True`, and the bot's file resolver couldn't reliably pick the user-intended one. Added `playlist_items='1'` to force yt-dlp to download only the outer tweet's video.

## [0.1.3] - 2026-05-05

### Fixed
- **X / Twitter download still failed** with `.NA` extension on the v0.1.2 image. Twitter's HTTP-format variants (which carry both video and audio in one progressive mp4) report `ext=NA` to yt-dlp because the codec is unknown until probed, and `merge_output_format` doesn't trigger for single-stream downloads. Added the `FFmpegVideoRemuxer` postprocessor to remux any non-mp4 output (including `.NA`) into `.mp4` after download.

### Changed
- **Snapchat story extractor is now more permissive across profile types.** v0.1.2 only searched for the `snapList` JSON key, which doesn't appear on every profile type. The walker now also picks up `storySnapList`, `publicStorySnapList`, and `snaps`, plus any list whose items contain a `mediaUrl` field. When extraction still finds nothing, the top-level `pageProps` keys are logged so the schema can be iterated on.

## [0.1.2] - 2026-05-05

### Fixed
- **YouTube videos and Shorts had no audio** when a quality was picked from the inline keyboard. yt-dlp's format selector was being given a video-only stream ID without instructions to merge audio. Now wraps the picked format as `<id>+bestaudio` so audio is merged in.
- **X / Twitter downloads failed** with `[Errno 2] No such file or directory: '...NA'`. The unconditional `FFmpegVideoConvertor` postprocessor was silently failing on Twitter HLS streams, leaving an unparseable `.NA` placeholder. Removed the postprocessor (yt-dlp's `merge_output_format=mp4` already produces mp4 cleanly) and switched file resolution to read `info["requested_downloads"]` instead of guessing extensions, with a wider extension fallback list (`mp4, mkv, webm, mov, m4v, ts`) for older yt-dlp versions.
- **Snapchat scraping always returned "user not found"**. Snapchat retired `story.snapchat.com/<username>` and migrated public profiles to `snapchat.com/@<username>` (with `snapchat.com/add/<username>` as an alias). Rewrote the scraper to load the new URL pattern, parse the embedded `__NEXT_DATA__` JSON, and extract the `snapList` array. Stories only — Spotlight is intentionally excluded.

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

[Unreleased]: https://github.com/ibrhoom/media-dl-bot/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.5
[0.1.4]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.4
[0.1.3]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.3
[0.1.2]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.2
[0.1.1]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.1
[0.1.0]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.0
