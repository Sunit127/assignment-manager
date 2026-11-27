#!/usr/bin/env python3
"""
Assignment Manager v3
=====================
A beautiful, client-first desktop app for tracking assignments and deadlines.

Architecture
- Python standard-library backend (SQLite + tiny JSON HTTP API on 127.0.0.1)
- Modern animated HTML/CSS/JS interface (ui/index.html)
- Native desktop window via pywebview (WKWebView on macOS, WebView2 on Windows);
  falls back to a Chrome/Edge "app mode" window, then to the default browser
- Background reminder thread -> native OS notifications
- Shares ~/.assignment_manager/assignments.db with the Claude extension

Run:  python3 assignment_manager.py            (opens the app window)
      python3 assignment_manager.py --server   (API + UI only, prints the URL)
"""

import csv
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_NAME = "Assignment Manager"
VERSION = "3.0.0"
DATE_FMT = "%Y-%m-%d %H:%M"
DAY_FMT = "%Y-%m-%d"
DATA_DIR = os.environ.get("ASSIGNMENT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".assignment_manager")
DB_PATH = os.path.join(DATA_DIR, "assignments.db")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
EMBEDDED_UI = None  # filled in by scripts/build.py for the single-file build

STATUSES = ["Not Started", "In Progress", "Review", "Submitted", "Completed", "Cancelled"]
PRIORITIES = ["Low", "Medium", "High", "Urgent"]
PAYMENTS = ["Unpaid", "Partially Paid", "Paid"]
DONE_STATUSES = {"Submitted", "Completed", "Cancelled"}
FIELDS = ["title", "client", "client_contact", "subject", "module_code", "word_count",
          "assigned_date", "deadline", "priority", "status", "amount", "payment_status",
          "description", "notes"]
DEFAULT_SETTINGS = {"soon_days": "3", "theme": "aurora", "mode": "dark", "owner": "",
                    "notifications": "1"}


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class Database:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, title TEXT NOT NULL,
                client TEXT, client_contact TEXT, subject TEXT, module_code TEXT,
                description TEXT, word_count TEXT, assigned_date TEXT, deadline TEXT NOT NULL,
                priority TEXT, status TEXT, amount TEXT, payment_status TEXT, notes TEXT,
                notified TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            """)
        self.conn.commit()

    # settings
    def settings(self):
        with self.lock:
            s = dict(DEFAULT_SETTINGS)
            s.update({r["key"]: r["value"] for r in
                      self.conn.execute("SELECT key, value FROM settings")})
            return s

    def save_settings(self, data):
        with self.lock:
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                                      (k, str(v)))
            self.conn.commit()

    # assignments
    def all(self):
        with self.lock:
            return [dict(r) for r in
                    self.conn.execute("SELECT * FROM assignments ORDER BY deadline, id")]

    def get(self, row_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM assignments WHERE id=?", (row_id,)).fetchone()
            return dict(r) if r else None

    def add(self, data):
        data = validate(data)
        data.setdefault("priority", "Medium")
        data.setdefault("status", "Not Started")
        data.setdefault("payment_status", "Unpaid")
        data.setdefault("assigned_date", datetime.now().strftime(DAY_FMT))
        now = datetime.now().strftime(DATE_FMT)
        with self.lock:
            cur = self.conn.execute(
                f"INSERT INTO assignments ({','.join(FIELDS)}, created_at, updated_at) "
                f"VALUES ({','.join('?' * len(FIELDS))}, ?, ?)",
                [data.get(f, "") for f in FIELDS] + [now, now])
            code = f"ASG-{datetime.now():%Y}-{cur.lastrowid:04d}"
            self.conn.execute("UPDATE assignments SET code=? WHERE id=?", (code, cur.lastrowid))
            self.conn.commit()
            return self.get(cur.lastrowid)

    def update(self, row_id, data):
        data = validate(data, partial=True)
        with self.lock:
            old = self.get(row_id)
            if not old:
                raise KeyError("Assignment not found")
            if not data:
                return old
            sets = ", ".join(f"{k}=?" for k in data)
            if "deadline" in data and data["deadline"] != old["deadline"]:
                sets += ", notified=''"
            self.conn.execute(f"UPDATE assignments SET {sets}, updated_at=? WHERE id=?",
                              list(data.values()) + [datetime.now().strftime(DATE_FMT), row_id])
            self.conn.commit()
            return self.get(row_id)

    def delete(self, row_id):
        with self.lock:
            self.conn.execute("DELETE FROM assignments WHERE id=?", (row_id,))
            self.conn.commit()

    def mark_notified(self, row_id, stages):
        with self.lock:
            row = self.get(row_id)
            cur = set(filter(None, (row["notified"] or "").split(",")))
            cur.update(stages)
            self.conn.execute("UPDATE assignments SET notified=? WHERE id=?",
                              (",".join(sorted(cur)), row_id))
            self.conn.commit()

    def signature(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, updated_at, status, deadline, notified FROM assignments "
                "ORDER BY id").fetchall()
            sets = self.conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return hashlib.md5(repr([tuple(r) for r in rows] + [tuple(s) for s in sets])
                           .encode()).hexdigest()


def parse_dt(text):
    text = (text or "").strip().replace("T", " ")
    for fmt in (DATE_FMT, "%Y-%m-%d %H:%M:%S", DAY_FMT):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(hour=23, minute=59) if fmt == DAY_FMT else dt
        except ValueError:
            pass
    return None


def validate(data, partial=False):
    clean = {}
    for k, v in (data or {}).items():
        if k not in FIELDS or v is None:
            continue
        v = str(v).strip()
        if k == "deadline":
            dt = parse_dt(v)
            if not dt:
                raise ValueError("Deadline must look like 2026-12-05 17:00")
            v = dt.strftime(DATE_FMT)
        elif k == "assigned_date" and v:
            dt = parse_dt(v)
            if not dt:
                raise ValueError("Assigned date must look like 2026-12-01")
            v = dt.strftime(DAY_FMT)
        elif k == "status" and v not in STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
        elif k == "priority" and v not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}")
        elif k == "payment_status" and v not in PAYMENTS:
            raise ValueError(f"Payment must be one of: {', '.join(PAYMENTS)}")
        clean[k] = v
    if not partial:
        if not clean.get("title"):
            raise ValueError("Title is required")
        if not clean.get("deadline"):
            raise ValueError("Deadline is required")
    return clean


# --------------------------------------------------------------------------- #
# Notifications & reminders
# --------------------------------------------------------------------------- #
def send_notification(title, message):
    text = message.replace("\n", " — ")[:250]
    try:
        if sys.platform == "darwin":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{esc(text)}" with title "{esc(APP_NAME)}" '
                 f'subtitle "{esc(title)}" sound name "Glass"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                  "ContentType = WindowsRuntime] > $null; "
                  "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                  "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                  f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) "
                  "> $null; "
                  f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode("
                  f"'{text.replace(chr(39), '')}')) > $null; "
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                  f"'{APP_NAME}').Show([Windows.UI.Notifications.ToastNotification]::new($t))")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", f"{APP_NAME}: {title}", text])
    except Exception:
        pass


def time_left(dl, now):
    secs = int((dl - now).total_seconds())
    over = secs < 0
    secs = abs(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    txt = f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")
    return f"overdue by {txt}" if over else f"{txt} left"


class Reminders(threading.Thread):
    ORDER = ["soon", "1d", "1h", "overdue"]

    def __init__(self, db, events):
        super().__init__(daemon=True)
        self.db = db
        self.events = events  # list shared with the UI (in-app toasts)

    def check(self):
        s = self.db.settings()
        soon_days = int(s.get("soon_days") or 3)
        now = datetime.now()
        for r in self.db.all():
            if r["status"] in DONE_STATUSES:
                continue
            dl = parse_dt(r["deadline"])
            if not dl:
                continue
            sent = set(filter(None, (r["notified"] or "").split(",")))
            rem = dl - now
            stage = None
            if rem.total_seconds() < 0:
                stage, title = "overdue", "Deadline passed"
            elif rem <= timedelta(hours=1):
                stage, title = "1h", "Due within 1 hour"
            elif rem <= timedelta(days=1):
                stage, title = "1d", "Due within 24 hours"
            elif rem <= timedelta(days=soon_days):
                stage, title = "soon", f"Due in {soon_days} days or less"
            if stage and stage not in sent:
                client = r["client"] or "No client"
                mod = f"{r['module_code']} – " if r["module_code"] else ""
                msg = f"{client}: {mod}{r['title']}\n{dl:%a %d %b, %H:%M} • {time_left(dl, now)}"
                if s.get("notifications", "1") == "1":
                    send_notification(title, msg)
                self.events.append({"t": time.time(), "title": title, "message": msg,
                                    "id": r["id"], "stage": stage})
                del self.events[:-30]
                self.db.mark_notified(r["id"], self.ORDER[: self.ORDER.index(stage) + 1])

    def run(self):
        time.sleep(2)
        while True:
            try:
                self.check()
            except Exception as e:  # never die
                print("reminder error:", e, file=sys.stderr)
            time.sleep(30)


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, db):
        self.db = db
        self.token = secrets.token_urlsafe(24)
        self.events = []
        self.last_ping = time.time()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def export_csv(self):
        folder = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(folder):
            folder = DATA_DIR
        path = os.path.join(folder, f"assignments_{datetime.now():%Y%m%d_%H%M}.csv")
        cols = ["code"] + FIELDS + ["created_at", "updated_at"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in self.db.all():
                w.writerow([r.get(c, "") for c in cols])
        return path

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return secrets.compare_digest(self.headers.get("X-Token", ""), server.token)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    ui_file = os.path.join(UI_DIR, "index.html")
                    if os.path.exists(ui_file):
                        with open(ui_file, "rb") as fh:
                            html = fh.read()
                    else:
                        html = (EMBEDDED_UI or "<h1>UI missing</h1>").encode("utf-8")
                    html = html.replace(b"__API_TOKEN__", server.token.encode())
                    return self._send(200, html, "text/html; charset=utf-8")
                if path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if not self._authed():
                    return self._send(403, {"error": "forbidden"})
                server.last_ping = time.time()
                if path == "/api/state":
                    return self._send(200, {
                        "assignments": server.db.all(),
                        "settings": server.db.settings(),
                        "signature": server.db.signature(),
                        "events": server.events[-10:],
                        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "version": VERSION,
                        "meta": {"statuses": STATUSES, "priorities": PRIORITIES,
                                 "payments": PAYMENTS, "done": sorted(DONE_STATUSES)}})
                if path == "/api/signature":
                    return self._send(200, {"signature": server.db.signature()})
                return self._send(404, {"error": "not found"})

            def _mutate(self, method):
                if not self._authed():
                    return self._send(403, {"error": "forbidden"})
                path = urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                try:
                    body = self._body()
                    if parts == ["api", "assignments"] and method == "POST":
                        return self._send(201, server.db.add(body))
                    if len(parts) == 3 and parts[:2] == ["api", "assignments"]:
                        rid = int(parts[2])
                        if method == "PUT":
                            return self._send(200, server.db.update(rid, body))
                        if method == "DELETE":
                            server.db.delete(rid)
                            return self._send(200, {"ok": True})
                    if parts == ["api", "settings"] and method == "PUT":
                        server.db.save_settings(body)
                        return self._send(200, server.db.settings())
                    if parts == ["api", "export"] and method == "POST":
                        return self._send(200, {"path": server.export_csv()})
                    if parts == ["api", "test-notification"] and method == "POST":
                        send_notification("Notifications are working",
                                          "You'll be reminded before every deadline.")
                        return self._send(200, {"ok": True})
                    return self._send(404, {"error": "not found"})
                except (ValueError, KeyError) as e:
                    return self._send(400, {"error": str(e).strip("'")})

            def do_POST(self):
                self._mutate("POST")

            def do_PUT(self):
                self._mutate("PUT")

            def do_DELETE(self):
                self._mutate("DELETE")

        return Handler

    def serve(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
def ensure_webview():
    """Make pywebview available: install it once into ~/.assignment_manager/venv and
    re-launch with that interpreter. Returns True if webview can be imported here."""
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        pass
    if "--no-webview" in sys.argv or os.environ.get("AM_NO_BOOTSTRAP"):
        return False
    win = sys.platform.startswith("win")
    venv = os.path.join(DATA_DIR, "venv")
    vpy = os.path.join(venv, "Scripts", "python.exe") if win else os.path.join(venv, "bin", "python")
    if os.path.realpath(sys.executable) == os.path.realpath(vpy) or os.environ.get("AM_IN_VENV"):
        return False  # already inside the venv and it still failed
    marker = os.path.join(DATA_DIR, "webview_failed")
    if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 86400:
        return False  # failed recently; don't slow every launch down
    os.makedirs(DATA_DIR, exist_ok=True)
    log = open(os.path.join(DATA_DIR, "setup.log"), "a")
    try:
        ok = os.path.exists(vpy) and subprocess.run(
            [vpy, "-c", "import webview"], stdout=log, stderr=log).returncode == 0
        if not ok:
            send_notification("Setting up", "Preparing the app window (first launch only, about a minute)…")
            if not os.path.exists(vpy):
                subprocess.run([sys.executable, "-m", "venv", venv], check=True, stdout=log,
                               stderr=log, timeout=300)
            subprocess.run([vpy, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                            "pywebview"], check=True, stdout=log, stderr=log, timeout=900)
        log.close()
        env = dict(os.environ, AM_IN_VENV="1")
        args = [vpy, os.path.abspath(__file__)] + sys.argv[1:]
        if win:
            sys.exit(subprocess.call(args, env=env))
        os.execve(vpy, args, env)
    except Exception as e:
        print(f"webview setup failed: {e}", file=log)
        log.close()
        open(marker, "w").close()
        return False


def open_app_window(url):
    """Native window via pywebview; fall back to a browser 'app' window."""
    try:
        import webview  # noqa: F401
        return "webview"
    except Exception:
        pass
    candidates = []
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            if os.path.isdir(f"/Applications/{app}.app"):
                candidates.append(["open", "-na", app, "--args", f"--app={url}",
                                   "--window-size=1400,880"])
    elif sys.platform.startswith("win"):
        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(exe):
                candidates.append([exe, f"--app={url}", "--window-size=1400,880"])
    else:
        for exe in ("google-chrome", "chromium", "chromium-browser"):
            if shutil.which(exe):
                candidates.append([exe, f"--app={url}"])
    for cmd in candidates:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "browser-app"
        except Exception:
            continue
    webbrowser.open(url)
    return "browser"


def main():
    if "--server" not in sys.argv:
        ensure_webview()  # may re-launch this script inside the private venv
    db = Database()
    server = Server(db)
    server.serve()
    Reminders(db, server.events).start()

    if "--server" in sys.argv:
        print(server.url, flush=True)
        while True:
            time.sleep(3600)

    mode = open_app_window(server.url)
    if mode == "webview":
        import webview
        webview.create_window(APP_NAME, server.url, width=1440, height=900,
                              min_size=(1100, 700), background_color="#0b1020",
                              text_select=True)
        webview.start()
        return
    # Browser fallback: keep running while the UI is open (it pings every 10 s).
    server.last_ping = time.time() + 60  # grace period for the window to open
    while time.time() - server.last_ping < 90:
        time.sleep(5)


if __name__ == "__main__":
    main()
