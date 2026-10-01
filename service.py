# -*- coding: utf-8 -*-
"""
Hanabi (hanabi.fan) Subtitles - Kodi subtitle service addon.

Uses Hanabi's official, documented REST API
(https://hanabi.fan/wp-json/hanabi/v1), which the site publishes so tools
like this can integrate without logging in through the site's own web
form. No web page scraping.

Auth: a personal access token, created by the user at Hanabi under
account settings -> "Pristupovy token", pasted into this addon's
settings. Sent as `Authorization: Bearer <token>` on every request,
including the download - never as a URL parameter, never logged.

Endpoints used:
  - GET /projects?query=<text>            - search projects by title
                                             (matches original/English
                                             title too); paginated via
                                             `page`/`total_pages`
  - GET /projects/{id}/subtitles?episode=N - subtitle releases for one
                                             episode of a project (omit
                                             `episode` for the full list);
                                             also paginated
  - GET /subtitles/{id}/download           - the actual file, as a ZIP
                                             (Content-Type: application/zip,
                                             not password-protected)

A project can have more than one subtitle release per episode (different
fansub groups or translation versions). All of them are listed rather
than picked automatically, since only the person watching knows which
release their video file is. Hanabi's project data has no season field,
so a search can also match more than one project (e.g. a combined project
and a separate "2nd Season" one) - see pick_candidate_projects() below.

Downloads: the API serves an already-existing ZIP from storage, normally
instantly; ~30 s is its worst-case timeout for that storage read, and
DOWNLOAD_TIMEOUT is sized so a slow read isn't cut off. A ZIP is capped
at 50 MB by the API, so downloads are streamed with their own size cap
(MAX_DOWNLOAD_BYTES).

Rate limits (per account, set by Hanabi): 60 requests/minute across the
search/listing endpoints, and 10 ZIP downloads/minute (60/hour, at most 2
at once). A 429 response carries a `Retry-After` header (seconds); see
http_get_with_retry() below.

No session/result caching beyond the short-lived per-search rows file.

Debugging: enable Kodi's debug log (Settings -> System -> Logging),
reproduce, then look for "[Hanabi]" lines in kodi.log. The token is never written to the log.
"""

import difflib
import io
import json
import os
import re
import shutil
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
OWNERS_FILE = os.path.join(TEMP_DIR, 'hanabi_owners.json')

HANDLE = int(sys.argv[1])

API_BASE = "https://hanabi.fan/wp-json/hanabi/v1"
REQUEST_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 40  # API doc: reading the ZIP from storage server-side can take up to ~30s in the worst case

LANG_NAME = "Czech"
LANG_FLAG = "cs"

# Hanabi's documented account-wide rate limit is 60 req/min for
# search/listing endpoints; a wait longer than this is reported to the
# user instead of blocking Kodi's UI thread for a long time.
MAX_RATE_LIMIT_WAIT = 60

# The API itself caps a ZIP at 50MB - refuse anything meaningfully bigger
# rather than trust a Content-Length header or stream indefinitely.
MAX_DOWNLOAD_BYTES = 55 * 1024 * 1024

# Zip-bomb guard: refuse to extract an archive whose *uncompressed*
# contents would be implausibly large for a subtitle file/pack.
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024

# Safety cap on how many subtitle rows get listed in one go, in case a
# search matches several ambiguous projects that each have many releases.
MAX_DISPLAY_ITEMS = 80

# Anything left behind in TEMP_DIR older than this gets swept on the next
# run - a search+download round trip finishes in well under a minute, so
# anything still there an hour later is leftover, not in-use.
TEMP_MAX_AGE_SECONDS = 3600


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


def current_video():
    """Path of the video Kodi has loaded - playing OR paused - else None.
    (Player.Playing alone isn't enough: it's false while paused.)"""
    try:
        if xbmc.getCondVisibility('Player.HasVideo') or xbmc.getCondVisibility('Player.Paused'):
            return xbmc.getInfoLabel('Player.Filenameandpath') or None
    except Exception:
        pass
    return None


def _read_owners():
    try:
        with open(OWNERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_owners(owners):
    try:
        with open(OWNERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(owners, f)
    except Exception as e:
        log("couldn't save {0}: {1}".format(OWNERS_FILE, e))


def remember_owner(path):
    """Record which video a delivered subtitle belongs to, so
    cleanup_temp_dir() never deletes it while that video is still loaded
    (e.g. paused for more than an hour)."""
    video = current_video()
    if not video:
        return
    try:
        top = os.path.relpath(path, TEMP_DIR).split(os.sep)[0]
    except ValueError:
        return
    if not top or top.startswith('..'):
        return
    owners = _read_owners()
    owners[top] = video
    _write_owners(owners)


def cleanup_temp_dir():
    """Sweep old downloads out of TEMP_DIR. Kept: the small caches managed by their own logic, and every
    subtitle belonging to the video Kodi currently has loaded - playing or
    paused - however old it is, so a long pause can't delete a subtitle
    that's still in use."""
    keep = {os.path.basename(ROWS_FILE), os.path.basename(OWNERS_FILE)}
    owners = _read_owners()
    video = current_video()
    try:
        now = time.time()
        for name in os.listdir(TEMP_DIR):
            if name in keep or not name.startswith('hanabi_'):
                continue
            if video and owners.get(name) == video:
                continue
            path = os.path.join(TEMP_DIR, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age < TEMP_MAX_AGE_SECONDS:
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except OSError as e:
                log("cleanup: couldn't remove {0}: {1}".format(path, e))
        existing = set(os.listdir(TEMP_DIR))
        still_there = dict((k, v) for k, v in owners.items() if k in existing)
        if still_there != owners:
            _write_owners(still_there)
    except Exception as e:
        log("cleanup_temp_dir failed: {0}".format(e))


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


# ---------------- title/query cleanup ----------------

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


def pick_candidate_projects(query, projects, max_candidates=5):
    """Score every project against the query (closest-title match, tried
    against title/original_title/english_title) and return every plausible candidate rather than
    silently picking one. Hanabi's project data has no season field, so a
    query like "Some Anime 2" can legitimately match both a combined
    project and a separate "2nd Season" project - in that case the user
    should choose, not the addon.

    Returns a list of projects, most-likely first. A single clear winner
    (a high ratio, comfortably ahead of the runner-up) still comes back as
    a one-item list, so callers only need one code path."""
    if not projects:
        return []
    query_lower = query.lower()
    scored = []
    for p in projects:
        best_ratio = -1.0
        for field in ('title', 'original_title', 'english_title'):
            candidate = (p.get(field) or '').strip()
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, query_lower, candidate.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
        scored.append((best_ratio, p))
    scored.sort(key=lambda t: t[0], reverse=True)

    top_ratio = scored[0][0]
    second_ratio = scored[1][0] if len(scored) > 1 else -1.0
    if top_ratio >= 0.92 and (top_ratio - second_ratio) >= 0.12:
        return [scored[0][1]]

    candidates = [p for ratio, p in scored if ratio >= 0.35][:max_candidates]
    return candidates or [scored[0][1]]


def get_allowed_languages(params):
    raw = params.get('languages', [''])[0]
    if not raw:
        return None
    return set(unquote(n) for n in raw.split(','))


# ---------------- API access ----------------

def _parse_retry_after(value):
    """Parse a Retry-After header value. Hanabi's docs describe it purely
    as a number of seconds (e.g. `Retry-After: 45`), not an HTTP-date, so
    that's all this handles; returns None if it can't be parsed."""
    if not value:
        return None
    try:
        seconds = int(str(value).strip())
        return seconds if seconds >= 0 else None
    except (TypeError, ValueError):
        return None


def http_get_with_retry(url, headers=None, params=None, timeout=REQUEST_TIMEOUT, _retried=False, **kwargs):
    """requests.get() wrapper that understands Hanabi's account-wide rate
    limiting. On HTTP 429 it reads the `Retry-After` header (seconds),
    tells the user how long that is, waits, and retries exactly once - a
    short wait is worth absorbing automatically, but a long one is
    reported instead of blocking Kodi's UI thread. Accepts the same
    keyword arguments as requests.get (e.g. stream=True for downloads)."""
    resp = requests.get(url, headers=headers, params=params, timeout=timeout, **kwargs)
    if resp.status_code == 429 and not _retried:
        wait_s = _parse_retry_after(resp.headers.get('Retry-After'))
        if wait_s is None:
            wait_s = 5
        if wait_s <= MAX_RATE_LIMIT_WAIT:
            notify("Hanabi rate limit hit - waiting {0}s...".format(wait_s))
            log("HTTP 429 from {0}, waiting {1}s per Retry-After then retrying once".format(url, wait_s))
            xbmc.sleep(wait_s * 1000)
            return http_get_with_retry(url, headers=headers, params=params, timeout=timeout, _retried=True, **kwargs)
        minutes = max(1, wait_s // 60)
        notify("Hanabi rate limit hit - try again in about {0} minute(s).".format(minutes))
        log("HTTP 429 from {0}, Retry-After={1}s exceeds MAX_RATE_LIMIT_WAIT, not retrying".format(url, wait_s))
    return resp


def api_get(path, params=None):
    """GET against the Hanabi API. Returns (ok, data_or_error_message)."""
    url = API_BASE + path
    try:
        resp = http_get_with_retry(url, headers=auth_headers(), params=params, timeout=REQUEST_TIMEOUT)
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
    if resp.status_code == 429:
        return False, "Hanabi rate limit hit - please wait a bit and try again."
    if resp.status_code == 401:
        return False, "Hanabi token missing/invalid or account not approved - check the addon settings."
    return False, msg


def api_get_all_pages(path, params=None, item_key='items', max_pages=10, max_items=None):
    """GET every page of a paginated Hanabi listing endpoint, following the
    API's own `page`/`total_pages` response fields, instead of only ever
    reading the first page. `max_pages`/`max_items` are safety caps
    against a runaway loop (an API bug, or an unexpectedly huge result),
    not limits expected to bite in normal use. If a later page fails, the
    items already collected are returned rather than thrown away."""
    all_items = []
    page = 1
    params = dict(params or {})
    per_page = params.get('per_page')
    while True:
        page_params = dict(params)
        page_params['page'] = page
        ok, data = api_get(path, page_params)
        if not ok:
            if all_items:
                log("pagination for {0} stopped early on page {1}: {2}".format(path, page, data))
                break
            notify(data)
            return []

        items = data.get(item_key) or [] if isinstance(data, dict) else []
        all_items.extend(items)

        if max_items is not None and len(all_items) >= max_items:
            all_items = all_items[:max_items]
            break
        if not items:
            break

        total_pages = data.get('total_pages') if isinstance(data, dict) else None
        if total_pages is not None:
            if page >= total_pages:
                break
        elif per_page is not None and len(items) < per_page:
            # No total_pages field to go by - a short page means it's the last one.
            break

        page += 1
        if page > max_pages:
            log("pagination for {0} hit max_pages={1}, stopping".format(path, max_pages))
            break

    return all_items


def search_projects(query):
    return api_get_all_pages("/projects", {"query": query, "per_page": 20}, max_pages=5, max_items=100)


def fetch_subtitles(project_id, episode=None):
    params = {"per_page": 50}
    if episode is not None:
        params["episode"] = episode
    return api_get_all_pages("/projects/{0}/subtitles".format(project_id), params, max_pages=6, max_items=300)


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
    # title (e.g. a "2nd Season" project). pick_candidate_projects() below
    # surfaces every plausible match instead of silently guessing one.

    projects = search_projects(query)
    log("{0} project(s) matched '{1}'".format(len(projects), query))
    if not projects:
        return

    candidates = pick_candidate_projects(query, projects)
    multi = len(candidates) > 1
    if multi:
        log("{0} ambiguous project candidate(s) for '{1}': {2}".format(
            len(candidates), query, [c.get('title') for c in candidates]))
    else:
        log("picked project: '{0}' (id={1})".format(candidates[0].get('title'), candidates[0].get('id')))

    allowed_langs = get_allowed_languages(params)
    saved = {}
    shown = 0
    for project in candidates:
        if shown >= MAX_DISPLAY_ITEMS:
            break
        rows = fetch_subtitles(project['id'], episode=episode)
        log("{0} subtitle release(s) found for '{1}'".format(len(rows), project.get('title')))
        for row in rows:
            if shown >= MAX_DISPLAY_ITEMS:
                log("hit MAX_DISPLAY_ITEMS={0}, not listing any more".format(MAX_DISPLAY_ITEMS))
                break
            lang_name = LANG_NAME if row.get('language') == 'cs' else (row.get('language') or LANG_NAME)
            if allowed_langs and lang_name not in allowed_langs:
                continue
            rid = str(len(saved))
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
            if multi:
                # More than one project matched ambiguously - prefix with
                # the project title so the user can tell them apart.
                label2 = "{0}: {1}".format(project.get('title') or '?', label2)
            append_subtitle(lang_name, LANG_FLAG if lang_name == LANG_NAME else '', label2, {"action": "download", "rid": rid})
            shown += 1

    save_json(ROWS_FILE, saved)
    log("listed {0} subtitle(s) for '{1}'".format(shown, query))


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
        resp = http_get_with_retry(download_url, headers=auth_headers(), timeout=DOWNLOAD_TIMEOUT, stream=True)
    except Exception as e:
        log("download failed: {0}".format(e))
        notify("Download failed (see debug log).")
        return

    try:
        if resp.status_code != 200:
            notify("Download failed: {0}".format(api_error_message(resp)))
            log("download got HTTP {0}".format(resp.status_code))
            return

        content_length = resp.headers.get('Content-Length')
        if content_length:
            try:
                if int(content_length) > MAX_DOWNLOAD_BYTES:
                    notify("Download refused - file is larger than expected (see debug log).")
                    log("download refused: Content-Length {0} exceeds MAX_DOWNLOAD_BYTES {1}".format(
                        content_length, MAX_DOWNLOAD_BYTES))
                    return
            except ValueError:
                pass

        # Stream and enforce the size cap as we go, rather than trusting
        # Content-Length alone (it can be absent or wrong) or buffering an
        # unbounded response straight into memory.
        buf = io.BytesIO()
        total = 0
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    notify("Download aborted - file is larger than expected (see debug log).")
                    log("download aborted after {0} bytes, exceeds MAX_DOWNLOAD_BYTES {1}".format(
                        total, MAX_DOWNLOAD_BYTES))
                    return
                buf.write(chunk)
        except Exception as e:
            log("download stream failed: {0}".format(e))
            notify("Download failed (see debug log).")
            return
    finally:
        resp.close()

    content = buf.getvalue()
    if not content:
        notify("Download failed - empty response (see debug log).")
        return

    # The API always returns a ZIP (Content-Type: application/zip) - a
    # non-ZIP body (an HTML error page, a proxy's own error page, ...) is
    # rejected outright rather than guessed-at and saved as a .srt,
    # which could silently hand Kodi garbage as a "subtitle".
    if not zipfile.is_zipfile(io.BytesIO(content)):
        notify("Download failed - response wasn't a valid subtitle archive (see debug log).")
        log("downloaded body isn't a valid zip (first bytes: {0!r})".format(content[:16]))
        return

    safe_name = re.sub(r'[^\w\-]+', '_', str(row.get('id', 'sub')))
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
            # Zip-bomb guard: check the *uncompressed* total before
            # extracting, not just the compressed download size.
            extracted_size = sum(info.file_size for info in zf.infolist())
            if extracted_size > MAX_EXTRACTED_BYTES:
                notify("Download refused - archive is larger than expected when extracted (see debug log).")
                log("refusing to extract {0}: extracted size {1} exceeds MAX_EXTRACTED_BYTES {2}".format(
                    zip_path, extracted_size, MAX_EXTRACTED_BYTES))
                return
            zf.extractall(extract_dir)
    except zipfile.BadZipFile as e:
        log("zip file is corrupt: {0}".format(e))
        notify("Downloaded file wasn't a valid archive (see debug log).")
        return
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

    remember_owner(sub_file)

    log("saved subtitle to {0}".format(sub_file))
    listitem = xbmcgui.ListItem(label=os.path.basename(sub_file))
    xbmcplugin.addDirectoryItem(handle=HANDLE, url=sub_file, listitem=listitem, isFolder=False)


def run():
    try:
        cleanup_temp_dir()
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
