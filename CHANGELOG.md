# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Released]

## [0.2.0] - 2026-07-19

Security-hardening release from a full audit, plus a large-file upload
fix found during verification. No user-facing feature changes.

### Security
- **Container now runs as non-root (`pwuser`) with Chromium's sandbox enabled.** Dropped `--no-sandbox`/`--disable-setuid-sandbox` from the Playwright launch and added Playwright's official seccomp profile (`seccomp_profile.json`, loaded via `security_opt`) so the sandbox's user-namespace syscalls are permitted. Untrusted Snapchat pages are no longer rendered as root with the sandbox off.
- **Bot token redacted from all log output**, including exception tracebacks. The existing httpx/httpcore silencing couldn't cover an exception whose text embeds a `/bot<TOKEN>/` URL logged with a traceback; a redacting log formatter now scrubs the token from every record on both handlers.
- **Bot API port 8081 no longer published to the host.** The bot reaches `telegram-bot-api` over the internal compose network; publishing on `0.0.0.0` exposed the token-driven API to the whole LAN.
- **Snapchat media/preview URLs restricted to an allowlist** (`*.sc-cdn.net`, `*.snapchat.com`, https only) before download — SSRF defense against a spoofed or changed `__NEXT_DATA__` schema pointing at internal/metadata addresses.
- **`ALLOWED_USERS` now enforced on both callback query handlers.** They were the only entry points without the auth guard.
- **Only the matched URL is passed to yt-dlp**, not the full message text, closing a path where a platform pattern matching inside a crafted string could hand a foreign URL to the generic extractor.
- **Scraped `snapIndex` coerced to int** before it reaches download filenames/captions; **input length bounded** (32-char username, 1024-char message).
- **Unused `API_ID`/`API_HASH` removed from the bot service** env (only `telegram-bot-api` needs them), and **generic error messages** sent to chat instead of raw exception text (which could leak internal paths/URLs).

### Fixed
- **Large files no longer report a false "Timed out".** The self-hosted Bot API relays the whole file to Telegram before responding, so the response wait scales with file size; python-telegram-bot's 5s default `read_timeout` reproducibly failed a 144 MB upload that then succeeded seconds later. The per-upload `read_timeout` now scales with the file size (clamped to 30–300s). Pre-existing since the initial commit.
- **Concurrent Snapchat downloads no longer collide.** Snap filenames gained a per-download uuid prefix; without it, two requests for the same username (or fast multi-taps) could share one path and one request's cleanup could delete a file another was still sending.

### Changed
- **Dependencies pinned and refreshed for reproducible builds:** Playwright `1.48.0 → 1.61.0` (with the base image `v1.48.0-noble → v1.61.0-noble`, refreshing a ~21-month-stale Chromium), aiohttp `3.10.10 → 3.14.1`, Pillow `>=10.0.0 → ==12.3.0`, yt-dlp now the pinned pip package `==2026.7.4` (the unpinned, unverified standalone binary fetch — and the `wget` that fetched it — were removed; the binary was never invoked). `telegram-bot-api` pinned to the `10.2` multi-arch digest.
- **Resource limits added** to both compose services (bot: 2 CPU / 2 GB, telegram-bot-api: 1 CPU / 1 GB) so a runaway process can't starve the host.
- **Disk-fill guard:** downloads exceeding the Telegram size cap are refused/aborted up front (yt-dlp `max_filesize`; streamed byte cap for Snapchat) instead of being fully written then rejected, and stale files are swept from the downloads dir at the start of each download.
- **Snapchat picker TTL reduced to 2 minutes** and the grid message is now auto-deleted from the chat on expiry or Close (previously the caption was edited and the image left in place).

### Deployment notes
- The container now runs as non-root, so host `downloads/` and `logs/` must be owned by `pwuser`'s uid (typically `1001`; confirm with `docker compose exec bot id`). `seccomp_profile.json` must be present next to the compose file. Both are covered by a normal `git pull`; see the README.

## [0.1.9] - 2026-05-17

### Changed
- **Snapchat picker now stays open after a single-snap download** so the user can keep grabbing more snaps from the same story without re-requesting it. Already-downloaded snaps are marked with ✅ in the keyboard, and the session TTL refreshes on every pick. `⭐ Download all` and `❌ Close` (renamed from "Cancel") end the session and remove the buttons. v0.1.8 closed the picker after any tap, which felt premature when the typical workflow is "pick 3, then 7, then 12".
- **Session TTL bumped from 2 → 5 minutes** to give the new multi-pick flow more breathing room. Each successful pick also resets the timer.

## [0.1.8] - 2026-05-17

### Added
- **Snapchat preview grid + picker.** `snapchat <username>` no longer auto-downloads every snap. Instead, the bot fetches the story listing, builds a numbered grid image from Snapchat's own 256px thumbnails, and posts it with inline buttons so the user can pick which snap(s) to download. Each cell shows the snap number (1, 2, 3...) over the thumbnail with a high-contrast pill so it reads on any content. A `⭐ Download all` button preserves the old "grab everything" flow with one tap.
- **Pagination** when a story has more than 12 snaps (3-column × 4-row layout per page). `⬅️ Prev` / `Next ➡️` step through pages; current page shown as `N/M` in the middle.
- **2-minute session timeout.** Picker state lives in-memory keyed by user + message ID and is purged after 120 seconds (or on cancel). Late button taps show "Session expired — send the username again" instead of failing silently. Sessions are also opportunistically swept whenever a new session is created or any callback fires, so memory doesn't grow.
- **`Pillow>=10.0.0`** runtime dependency for compositing the grid PNG. Adds ~5 MB to the image.

### Changed
- `download_story_media` was refactored: the per-snap download loop is now a reusable helper (`_download_single_snap`) so the grid picker (single snap) and "Download all" share the same code path. Behaviour for "Download all" is unchanged.
- `fetch_snapchat_stories` now also extracts `mediaPreviewUrl.value` per snap (the 256px thumbnail) so the grid can be built without downloading the full media.

## [0.1.7] - 2026-05-06

### Added
- **Snapchat captions now include story order and post timestamp** as a three-line caption attached to each downloaded item: `👻 @username` / `N of M` / `2026-04-25 01:56pm`. The order uses Snapchat's own `snapIndex+1` so gaps are visible if a snap fails to download (matches what the user would see in the Snapchat app), and the total is the full story length, not what we managed to download.
- **`TZ` environment variable** (optional, default `Asia/Riyadh`) for the timezone used when rendering the post timestamp. Accepts any IANA timezone name (e.g. `America/New_York`, `Europe/London`, `UTC`). Invalid values are caught at startup and fall back to UTC with a warning. If Snapchat omits the timestamp on a snap, the date line is omitted rather than showing a 1970 placeholder.

## [0.1.6] - 2026-05-06

### Fixed
- **Snapchat scraping returned zero stories on every profile** since v0.1.2. The walker was looking for top-level keys like `mediaUrl` and `mediaType` on each snap, but Snapchat's actual schema nests the URL inside `snapUrls.mediaUrl` and uses an integer enum `snapMediaType` (0=image, 1=video). Verified the real path against `pageProps.story.snapList` on three profile types (news/business `iamoktsr`, creator `ifiii99`, personal `rola_lola94`); replaced the recursive walker with a targeted reader that only reads from the verified path. Active stories now download correctly across all profile types.

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

[Unreleased]: https://github.com/ibrhoom/media-dl-bot/compare/v0.1.9...HEAD
[0.1.9]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.9
[0.1.8]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.8
[0.1.7]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.7
[0.1.6]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.6
[0.1.5]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.5
[0.1.4]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.4
[0.1.3]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.3
[0.1.2]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.2
[0.1.1]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.1
[0.1.0]: https://github.com/ibrhoom/media-dl-bot/releases/tag/v0.1.0
