# Hanabi Subtitles - Kodi Subtitle Addon

Kodi subtitle service addon for hanabi.fan — Czech anime subtitles, via Hanabi's official REST API rather than site scraping.

Compatible with Kodi 19, 20, and 21.

## Current Version
service.subtitles.hanabi - 1.1.1

## Installation Instructions
Recommended: install through the [Highflight Subtitles Repository](https://github.com/KiritoSenpaiCZ/KiritoSenpaiCZ.github.io), which also handles updates.

Manual install:
1. Download `service.subtitles.hanabi-1.1.1.zip` from this repo (or build it from source)
2. In Kodi: **Add-ons > Install from zip file**, select the zip

## Setup Instructions
1. Log in at hanabi.fan, go to your account settings, open the "Přístupový token" tab, and generate a personal access token
2. In Kodi, open this addon's settings and paste the token in

The token is only ever sent as an `Authorization: Bearer` header — never in a URL or written to the debug log.

## How it works
- Matches Kodi's video metadata (or a manual search) against Hanabi's project catalog
- Lists every available subtitle release for the matching episode, since different fansub groups/versions show up separately
- Downloads and extracts the matching release's zip automatically
- Hanabi doesn't build the zip on request — it loads an existing one from storage and passes it through, so a download is normally instant. The addon's download step still uses a ~40 second timeout, matching the API's own worst-case (not typical) timeout for that storage read
- If Hanabi's account-wide rate limit is hit, the addon waits out a short cooldown automatically and tells you if a longer wait is needed

## Issues
Please open an issue in this repo with a description of the problem and, if possible, a Kodi debug log (Settings > System > Logging > Enable debug logging, then grep `kodi.log` for `[Hanabi]`).

## FAQ
**Q: My token stopped working.**
**A:** Generate a new one in your hanabi.fan account settings and update it in the addon's settings.

**Q: Can I use this without a hanabi.fan account?**
**A:** No, a personal access token from a hanabi.fan account is required.
