# Kodi-Hanabi

A Kodi subtitle addon (`service.subtitles.hanabi`) for [Hanabi](https://hanabi.fan/) — Czech anime subtitles.

Unlike the other addons in this family, this one talks to **Hanabi's own official REST API**, not site scraping — Hanabi's site owner built and published it (with a full spec and example responses) specifically so third-party tools could integrate, since the site's own login page is protected by Cloudflare Turnstile and can't be automated the way the others' login forms are.

## What it does

- Matches Kodi's video metadata (show title, season, episode) - or a manual search - against Hanabi's project catalog via its search API
- Lists every available subtitle release for the matching episode (different fansub release groups/versions show up separately, since only you know which release your video file actually is)
- Downloads and extracts the matching subtitle's ZIP archive automatically

## Installation

1. Create a personal access token at hanabi.fan: log in, go to your account settings, open the **"Přístupový token"** tab, and generate one
2. Download this repo as a zip, or build `service.subtitles.hanabi-1.0.0.zip` from its contents
3. In Kodi: **Add-ons → Install from zip file**, select the zip
4. Open the addon's settings and paste in your access token

## How it works

- All requests (search, episode listing, download) send `Authorization: Bearer <your token>` - the token is never put in a URL or written to the debug log
- Verified live end-to-end while building this addon: auth-check, project search (by title and by MAL ID), episode subtitle listing, and an actual file download all returned exactly what Hanabi's own API.md documentation describes - down to byte-for-byte matching the documented example responses
- The download endpoint can take up to ~30s to build the ZIP server-side (per Hanabi's own docs), so that request uses a longer timeout than the others

## Related

- [Kodi-Hiyori](https://github.com/KiritoSenpaiCZ/Kodi-Hiyori) — hiyori.cz
- [Kodi-Wosir](https://github.com/KiritoSenpaiCZ/Kodi-Wosir) — wosir.cz
- [Kodi-Edna](https://github.com/KiritoSenpaiCZ/Kodi-Edna) — edna.cz
- [Kodi-Kamui](https://github.com/KiritoSenpaiCZ/Kodi-Kamui) — kamui-subs.cz
- [Kodi-LegieKondor](https://github.com/KiritoSenpaiCZ/Kodi-LegieKondor) — anime4.legiekondor.cz
- [Kodi-NyaSub](https://github.com/KiritoSenpaiCZ/Kodi-NyaSub) — nyasub.cz
