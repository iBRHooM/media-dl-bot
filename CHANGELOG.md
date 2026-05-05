### Security
- **Bot token was being logged in plaintext** by `httpx`. Every Telegram API call wrote the full token to `bot.log`. Silenced the httpx logger. **Anyone running v0.1.0 – v0.1.3 should rotate their bot token via `/revoke` in @BotFather.**

### Fixed
- **Twitter quote-tweet downloads failed** when both the outer tweet and the quoted tweet had video. Added `playlist_items='1'` to download only the outer tweet's video.

### Docker image
- `ghcr.io/ibrhoom/media-dl-bot:0.1.4`

**Full Changelog:** https://github.com/iBRHooM/media-dl-bot/compare/v0.1.3...v0.1.4
