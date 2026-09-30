# -*- coding: utf-8 -*-
"""
Hanabi (hanabi.fan) Subtitle Downloader - Kodi subtitle service addon.

Unlike every other addon in this family, this one talks to a real,
documented REST API (https://hanabi.fan/wp-json/hanabi/v1) rather than
scraping HTML - Hanabi's own site owner built and published it (with a
full API.md spec and real example responses) specifically so third-party
tools like this addon could integrate without needing to log in through
the site's own web form (which is protected by Cloudflare Turnstile and
was, for that reason, never scraped directly by this addon or any other
in this family).

Auth: a personal access token, created by the user at Hanabi under
account settings -> "Pristupovy token", pasted into this addon's
settings. Sent as `Authorization: Bearer <token>` on every request,
including the download - never as a URL parameter, never logged.

Endpoints used (see the site's own API.md for the authoritative spec):
  - GET /projects?query=<text>            - search projects by title
                                             (matches original/English
                                             title too)
  - GET /projects/{id}/subtitles?episode=N - subtitle releases for one
                                             episode of a project (omit
                                             `episode` for the full list)
  - GET /subtitles/{id}/download           - the actual file, as a ZIP
                                             (Content-Type: application/zip,
                                             not password-protected)

A project can have more than one subtitle release per episode (different
fansub release groups, or different translation versions) - all of them
are listed rather than picked automatically, the same way WoSir lists
separate TV/BD releases, since only the person watching knows which
release their video file actually is.

Per the API doc: the ZIP download can take up to ~30s to build
server-side, so that request uses a longer timeout than the other calls;
a ZIP is capped at 50MB by the API itself.

Design choices carried over on purpose from Hiyori/WoSir/Edna/Kamui:
  - No session/result caching beyond the short-lived per-search rows
    file - simplicity over performance.
  - Heavy debug logging via log() - enable Kodi's debug log
    (Settings -> System -> Logging), reproduce, then grep kodi.log for
    "[Hanabi]" and paste the lines back for troubleshooting. The token
    itself is never written to the log.
"""

import difflib
import json
import os
import re
import sys
import time
import traceback
import zipfile
from urllib.parse import parse_qs, unquote

import requests
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')

PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
TEMP_DIR = xbmcvfs.translatePath(os.path.join(PROFILE, 'temp', ''))
if not xbmcvfs.exists(TEMP_DIR):
    xbmcvfs.mkdirs(TEMP_DIR)
ROWS_FILE = os.path.join(TEMP_DIR, 'hanabi_rows.json')

HANDLE = int(sys.argv[1])

API_BASE = "https://hanabi.fan/wp-json/hanabi/v1"
REQUEST_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 40  # API doc: building the ZIP server-side can take ~30s

LANG_NAME = "Czech"
LANG_FLAG = "cs"


# ---------------- small helpers ----------------

def log(msg):
    xbmc.log("[Hanabi] {0}".format(msg), level=xbmc.LOGDEBUG)


def notify(msg):
    xbmcgui.Dialog().notification(ADDON_NAME, msg, xbmcgui.NOTIFICATION_INFO, 4000)


def get_params():
    raw = sys.argv[2] if len(sys.argv) > 2 else ""
    return parse_qs(raw.lstrip('?'))


def save_json(path, data):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        log("failed to save {0}: {1}".format(path, e))


def load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def auth_headers():
    token = ADDON.getSetting('token').strip()
    return {"Authorization": "Bearer {0}".format(token)} if token else {}


def api_error_message(resp):
    """Best-effort human-readable message from one of the API's documented
    JSON error bodies ({code, message, data:{status}}); falls back to the
    raw status code if the body isn't in that shape."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get('message'):
            return body['message']
    except Exception:
        pass
    return "HTTP {0}".format(resp.status_code)


# ---------------- title/query cleanup (same logic as Hiyori/WoSir/Edna) ----------------

def clean_release_title(name):
    if not name:
        return name
    name = re.sub(r'\.\w+$', '', name)
    name = re.sub(r'\[[^\]]*\]', ' ', name)
    name = re.sub(r'\([^)]*\)', ' ', name)
    name = re.sub(r'[._]', ' ', name)

    lower = name.lower()
    cut_at = None
    m = re.search(r's\d{1,2}e\d{1,2}', lower)
    if m:
        cut_at = m.start()
    keywords = [
        "2160p", "1080p", "720p", "480p", "4k",
        "blu-ray", "bluray", "bdrip", "webrip", "web-dl", "web dl",
        "hdtv", "dvdrip", "hdrip",
        "x264", "x265", "h264", "h265", "hevc", "avc",
        "dual audio", "dual-audio", "multi audio", "multi-audio",
        "aac", "flac", "dts", "opus",
    ]
    for kw in keywords:
        idx = lower.find(kw)
        if idx != -1 and (cut_at is None or idx < cut_at):
            cut_at = idx
    if cut_at is not None:
        name = name[:cut_at]

    name = re.sub(r'\s-\s*\d+.*$', '', name)
    name = re.sub(r'[-–—]+\s*$', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def extract_season_episode(text):
    if not text:
        return text, None, None
    m = re.search(r'[sS](\d{1,2})[eE](\d{1,3})', text)
    if not m:
        return text, None, None
    season = int(m.group(1))
    episode = int(m.group(2))
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    cleaned = clean_release_title(cleaned) or cleaned
    return cleaned, season, episode


def pick_best_project(query, projects):
    """Case-insensitive closest-title match, tried against each of the
    title/original_title/english_title fields the API returns (a query
    can match any of the three server-side) - picks whichever project has
    the single closest match across all three."""
    if not projects:
        return None
    query_lower = query.lower()
    best = None
    best_ratio = -1.0
    for p in projects:
        for field in ('title', 'original_title', 'english_title'):
            candidate = (p.get(field) or '').strip()
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, query_lower, candidate.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = p
    return best or projects[0]


def get_allowed_languages(params):
    raw = params.get('languages', [''])[0]
    if not raw:
        return None
    return set(unquote(n) for n in raw.split(','))


# ---------------- API access ----------------

def api_get(path, params=None):
    """GET against the Hanabi API. Returns (ok, data_or_error_message)."""
    url = API_BASE + path
    try:
        resp = requests.get(url, headers=auth_headers(), params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        log("request to {0} failed: {1}".format(path, e))
        return False, "Network error contacting Hanabi (see debug log)."

    if resp.status_code == 200:
        try:
            return True, resp.json()
        except Exception as e:
            log("could not parse JSON from {0}: {1}".format(path, e))
            return False, "Unexpected response from Hanabi (see debug log)."

    msg = api_error_message(resp)
    log("{0} -> HTTP {1}: {2}".format(path, resp.status_code, msg))
    if resp.status_code == 401:
        return False, "Hanabi token missing/invalid or account not approved - check the addon settings."
    return False, msg


def search_projects(query):
    ok, data = api_get("/projects", {"query": query, "per_page": 20})
    if not ok:
        notify(data)
        return []
    return data.get('items', [])


def fetch_subtitles(project_id, episode=None):
    params = {"per_page": 50}
    if episode is not None:
        params["episode"] = episode
    ok, data = api_get("/projects/{0}/subtitles".format(project_id), params)
    if not ok:
        notify(data)
        return []
    return data.get('items', [])


def guess_extension(body_sample):
    try:
        sample_text = body_sample.decode('utf-8', 'ignore')
    except Exception:
        sample_text = ""
    if sample_text.strip().startswith("[Script Info]"):
        return ".ass"
    return ".srt"


# ---------------- Kodi-facing actions ----------------

def append_subtitle(lang_name, flag_code, label2, url_params):
    listitem = xbmcgui.ListItem(label=lang_name, label2=label2)
    if flag_code:
        listitem.setArt({"thumb": flag_code})
    listitem.setProperty("sync", "false")
    listitem.setProperty("hearing_imp", "false")
    url = "plugin://{0}/?{1}".format(
        ADDON_ID,
        "&".join("{0}={1}".format(k, requests.utils.quote(str(v))) for k, v in url_params.items())
    )
    xbmcplugin.addDirectoryItem(handle=HANDLE, url=url, listitem=listitem, isFolder=False)


def handle_search(params, is_manual):
    token = ADDON.getSetting('token').strip()
    if not token:
        notify("Set your Hanabi access token in the addon settings.")
        ADDON.openSettings()
        return

    season = episode = None
    if is_manual:
        query_raw = params.get('searchstring', [''])[0]
        query, season, episode = extract_season_episode(query_raw)
        if not query:
            query = clean_release_title(query_raw)
    else:
        tvshow = xbmc.getInfoLabel("VideoPlayer.TVshowtitle")
        title = tvshow or xbmc.getInfoLabel("VideoPlayer.OriginalTitle") or xbmc.getInfoLabel("VideoPlayer.Title")
        season_label = xbmc.getInfoLabel("VideoPlayer.Season")
        episode_label = xbmc.getInfoLabel("VideoPlayer.Episode")
        if season_label.isdigit():
            season = int(season_label)
        if episode_label.isdigit():
            episode = int(episode_label)

        if title:
            query = clean_release_title(title)
        else:
            try:
                filename = os.path.basename(unquote(xbmc.Player().getPlayingFile()))
            except Exception:
                filename = ""
            query, s2, e2 = extract_season_episode(filename)
            query = clean_release_title(query)
            if season is None:
                season = s2
            if episode is None:
                episode = e2

    query = (query or "").strip()
    if not query:
        log("no usable search query, aborting")
        return

    log("query='{0}' season={1} episode={2} manual={3}".format(query, season, episode, is_manual))
    # Hanabi's own project data has no season field (always null per the
    # API doc) - a numbered season, if any, is part of the project's own
    # title (e.g. a "2nd Season" project), which pick_best_project() can
    # only match if the cleaned query happens to overlap with it. Not
    # perfect, flagged here rather than silently assumed correct.

    projects = search_projects(query)
    log("{0} project(s) matched '{1}'".format(len(projects), query))
    if not projects:
        return

    project = pick_best_project(query, projects)
    log("picked project: '{0}' (id={1})".format(project.get('title'), project.get('id')))

    rows = fetch_subtitles(project['id'], episode=episode)
    log("{0} subtitle release(s) found".format(len(rows)))
    if not rows:
        return

    allowed_langs = get_allowed_languages(params)
    saved = {}
    shown = 0
    for i, row in enumerate(rows):
        lang_name = LANG_NAME if row.get('language') == 'cs' else (row.get('language') or LANG_NAME)
        if allowed_langs and lang_name not in allowed_langs:
            continue
        rid = str(i)
        saved[rid] = row
        ep = row.get('episode')
        ep_label = "E{0}".format(ep) if ep is not None else "(pack)"
        version = row.get('version')
        note = row.get('note')
        label2 = "{0} - {1}{2}{3}".format(
            ep_label,
            row.get('release') or '?',
            " v{0}".format(version) if version else "",
            " ({0})".format(note) if note else "",
        )
        append_subtitle(lang_name, LANG_FLAG if lang_name == LANG_NAME else '', label2, {"action": "download", "rid": rid})
        shown += 1

    save_json(ROWS_FILE, saved)
    log("listed {0} subtitle(s) for '{1}'".format(shown, project.get('title')))


def handle_download(params):
    rid = params.get('rid', [None])[0]
    if rid is None:
        notify("Nothing to download.")
        return
    rows = load_json(ROWS_FILE) or {}
    row = rows.get(rid)
    if not row:
        notify("Subtitle info expired - please search again.")
        return

    download_url = row.get('download_url')
    if not download_url:
        notify("No download link for this subtitle (see debug log).")
        log("row has no download_url: {0}".format({k: v for k, v in row.items() if k != 'download_url'}))
        return

    try:
        resp = requests.get(download_url, headers=auth_headers(), timeout=DOWNLOAD_TIMEOUT)
    except Exception as e:
        log("download failed: {0}".format(e))
        notify("Download failed (see debug log).")
        return

    if resp.status_code != 200:
        notify("Download failed: {0}".format(api_error_message(resp)))
        log("download got HTTP {0}".format(resp.status_code))
        return

    content = resp.content
    if not content:
        notify("Download failed - empty response (see debug log).")
        return

    safe_name = re.sub(r'[^\w\-]+', '_', str(row.get('id', 'sub')))
    is_zip = content[:2] == b'PK'

    if is_zip:
        zip_path = os.path.join(TEMP_DIR, "hanabi_{0}_{1}.zip".format(safe_name, int(time.time())))
        try:
            with open(zip_path, 'wb') as f:
                f.write(content)
        except Exception as e:
            log("failed to write zip file: {0}".format(e))
            notify("Downloaded but couldn't save the file (see debug log).")
            return

        extract_dir = os.path.join(TEMP_DIR, "hanabi_{0}_{1}".format(safe_name, int(time.time())))
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        except Exception as e:
            log("zip extract failed: {0}".format(e))
            notify("Downloaded a zip but couldn't extract it (see debug log).")
            return

        sub_file = None
        for root, _dirs, files in os.walk(extract_dir):
            for fn in files:
                if fn.lower().endswith(('.srt', '.ass', '.sub')):
                    sub_file = os.path.join(root, fn)
                    break
            if sub_file:
                break
        if not sub_file:
            notify("Downloaded and extracted, but no .srt/.ass file found inside.")
            log("no subtitle file found after extracting {0}".format(zip_path))
            return
        filepath = sub_file
    else:
        # Not expected per the API doc (always application/zip), but
        # handled defensively the same way as every other addon here.
        ext = guess_extension(content[:200])
        filename = "hanabi_{0}_{1}{2}".format(safe_name, int(time.time()), ext)
        filepath = os.path.join(TEMP_DIR, filename)
        try:
            with open(filepath, 'wb') as f:
                f.write(content)
        except Exception as e:
            log("failed to write subtitle file: {0}".format(e))
            notify("Downloaded but couldn't save the file (see debug log).")
            return

    log("saved subtitle to {0}".format(filepath))
    listitem = xbmcgui.ListItem(label=os.path.basename(filepath))
    xbmcplugin.addDirectoryItem(handle=HANDLE, url=filepath, listitem=listitem, isFolder=False)


def run():
    try:
        params = get_params()
        action = params.get('action', [''])[0]
        log("action={0} params={1}".format(action, params))
        if action == 'search':
            handle_search(params, is_manual=False)
        elif action == 'manualsearch':
            handle_search(params, is_manual=True)
        elif action == 'download':
            handle_download(params)
        else:
            log("unknown/missing action, nothing to do")
    except Exception as e:
        log("unhandled exception: {0}\n{1}".format(e, traceback.format_exc()))
    finally:
        xbmcplugin.endOfDirectory(HANDLE)


if __name__ == '__main__':
    run()
