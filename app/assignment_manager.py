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

import base64
import csv
import hashlib
import json
import os
import secrets
import ssl
import urllib.error
import urllib.request
import uuid
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
VERSION = "3.1.0"
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
                    "notifications": "1", "gh_token": "", "gh_repo": ""}
SYNC_FIELDS = ["code"] + FIELDS + ["created_at", "updated_at"]
SYNC_FILE = "assignments.json"


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
            CREATE TABLE IF NOT EXISTS tombstones (uid TEXT PRIMARY KEY, deleted_ms INTEGER);
            CREATE TABLE IF NOT EXISTS sync_state (uid TEXT PRIMARY KEY, hash TEXT);
            """)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(assignments)")}
        if "uid" not in cols:
            self.conn.execute("ALTER TABLE assignments ADD COLUMN uid TEXT")
        if "mtime" not in cols:
            self.conn.execute("ALTER TABLE assignments ADD COLUMN mtime INTEGER DEFAULT 0")
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

    def next_code(self, year=None):
        year = year or datetime.now().year
        best = 0
        for (code,) in self.conn.execute("SELECT code FROM assignments WHERE code LIKE ?",
                                         (f"ASG-{year}-%",)):
            try:
                best = max(best, int(str(code).rsplit("-", 1)[1]))
            except (ValueError, IndexError):
                pass
        return f"ASG-{year}-{best + 1:04d}"

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
            code = self.next_code()
            self.conn.execute("UPDATE assignments SET code=?, uid=? WHERE id=?",
                              (code, uuid.uuid4().hex, cur.lastrowid))
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

    # ------------------------------------------------------------ sync
    @staticmethod
    def rec_hash(r):
        return hashlib.sha1(json.dumps([str(r.get(k) or "") for k in SYNC_FIELDS
                                        if k != "updated_at"]).encode()).hexdigest()

    def prepare_sync(self):
        """Give every row a uid, stamp locally-changed rows with mtime=now and turn
        rows deleted since the last sync into tombstones. Returns (records, tombs)."""
        now = int(time.time() * 1000)
        with self.lock:
            rows = self.all()
            for r in rows:
                if not r.get("uid"):
                    r["uid"] = uuid.uuid4().hex
                    self.conn.execute("UPDATE assignments SET uid=? WHERE id=?", (r["uid"], r["id"]))
            state = dict(self.conn.execute("SELECT uid, hash FROM sync_state").fetchall())
            for r in rows:
                if state.get(r["uid"]) != self.rec_hash(r):
                    r["mtime"] = max(now, int(r.get("mtime") or 0))
                    self.conn.execute("UPDATE assignments SET mtime=? WHERE id=?", (r["mtime"], r["id"]))
            live = {r["uid"] for r in rows}
            tombs = dict(self.conn.execute("SELECT uid, deleted_ms FROM tombstones").fetchall())
            for uid in state:
                if uid not in live and uid not in tombs:
                    tombs[uid] = now
                    self.conn.execute("INSERT OR REPLACE INTO tombstones VALUES (?, ?)", (uid, now))
            self.conn.commit()
            recs = {}
            for r in rows:
                rec = {k: r.get(k) or "" for k in SYNC_FIELDS}
                rec["uid"], rec["mtime"] = r["uid"], int(r.get("mtime") or 0)
                recs[r["uid"]] = rec
            return recs, tombs

    def apply_sync(self, recs, tombs):
        """Make the local database match the merged result."""
        changed = False
        with self.lock:
            local = {r["uid"]: r for r in self.all() if r.get("uid")}
            for uid, r in local.items():
                if uid not in recs:
                    self.conn.execute("DELETE FROM assignments WHERE id=?", (r["id"],))
                    changed = True
            for uid, rec in recs.items():
                cur = local.get(uid)
                vals = {k: str(rec.get(k) or "") for k in SYNC_FIELDS}
                if cur is None:
                    if self.conn.execute("SELECT 1 FROM assignments WHERE code=?", (vals["code"],)).fetchone() \
                            or not vals["code"]:
                        vals["code"] = self.next_code()
                    cols = list(vals) + ["uid", "mtime", "notified"]
                    self.conn.execute(
                        f"INSERT INTO assignments ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        list(vals.values()) + [uid, int(rec.get("mtime") or 0), ""])
                    changed = True
                elif int(rec.get("mtime") or 0) > int(cur.get("mtime") or 0) and \
                        self.rec_hash(cur) != self.rec_hash(rec):
                    if vals["code"] != cur["code"] and self.conn.execute(
                            "SELECT 1 FROM assignments WHERE code=? AND id<>?",
                            (vals["code"], cur["id"])).fetchone():
                        vals["code"] = cur["code"]
                    notified = cur["notified"] if vals["deadline"] == cur["deadline"] else ""
                    self.conn.execute(
                        f"UPDATE assignments SET {', '.join(k + '=?' for k in vals)}, mtime=?, notified=? "
                        f"WHERE id=?", list(vals.values()) + [int(rec["mtime"]), notified, cur["id"]])
                    changed = True
            cutoff = int(time.time() * 1000) - 120 * 86400 * 1000
            self.conn.execute("DELETE FROM tombstones")
            for uid, ms in tombs.items():
                if ms >= cutoff:
                    self.conn.execute("INSERT INTO tombstones VALUES (?, ?)", (uid, int(ms)))
            self.conn.execute("DELETE FROM sync_state")
            for r in self.all():
                self.conn.execute("INSERT INTO sync_state VALUES (?, ?)", (r["uid"], self.rec_hash(r)))
            self.conn.commit()
        return changed

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
# GitHub sync (shared with the iPhone app)
# --------------------------------------------------------------------------- #
GH_API = os.environ.get("ASSIGNMENT_GH_API", "https://api.github.com")


def public_settings(s):
    out = dict(s)
    out["gh_token_set"] = bool(out.get("gh_token"))
    out["gh_token"] = ""
    return out


def merge_docs(L, LT, R, RT):
    """Last-writer-wins per record (by mtime); tombstones beat older edits.
    Identical logic lives in the iPhone app."""
    tombs = {u: max(int(LT.get(u, 0) or 0), int(RT.get(u, 0) or 0)) for u in set(LT) | set(RT)}
    recs = {}
    for u in set(L) | set(R):
        a, b = L.get(u), R.get(u)
        w = a if (b is None or (a is not None and int(a.get("mtime") or 0) >= int(b.get("mtime") or 0))) else b
        t = tombs.get(u)
        if t and t >= int(w.get("mtime") or 0):
            continue
        recs[u] = w
    return recs, tombs


def canonical(recs, tombs):
    return json.dumps({"a": [recs[u] for u in sorted(recs)],
                       "t": {u: tombs[u] for u in sorted(tombs)}}, sort_keys=True)


_SSL = None


def ssl_context():
    global _SSL
    if _SSL is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        if sys.platform == "darwin":  # python.org builds may lack a CA bundle
            try:
                pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                                      "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                     capture_output=True, text=True, timeout=20).stdout
                if pem:
                    ctx.load_verify_locations(cadata=pem)
            except Exception:
                pass
        _SSL = ctx
    return _SSL


class GitHubSync(threading.Thread):
    INTERVAL = 30

    def __init__(self, db):
        super().__init__(daemon=True)
        self.db = db
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.status = {"state": "off", "last": None, "error": None, "repo": ""}

    def _req(self, method, url, token, body=None):
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "User-Agent": "AssignmentManager",
                                              "Content-Type": "application/json"})
        ctx = ssl_context() if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            return json.loads(r.read() or b"{}")

    def sync_once(self):
        s = self.db.settings()
        token, repo = s.get("gh_token", "").strip(), s.get("gh_repo", "").strip().strip("/")
        if not token or not repo:
            self.status = {"state": "off", "last": None, "error": None, "repo": repo}
            return
        url = f"{GH_API}/repos/{repo}/contents/{SYNC_FILE}"
        with self.lock:
            self.status.update(state="syncing", repo=repo)
            for attempt in range(3):
                try:
                    local, ltombs = self.db.prepare_sync()
                    sha, remote, rtombs = None, {}, {}
                    try:
                        meta = self._req("GET", url, token)
                        sha = meta.get("sha")
                        doc = json.loads(base64.b64decode(meta.get("content", "")).decode("utf-8") or "{}")
                        remote = {r["uid"]: r for r in doc.get("assignments", []) if r.get("uid")}
                        rtombs = {t["uid"]: t["deleted_ms"] for t in doc.get("tombstones", [])}
                    except urllib.error.HTTPError as e:
                        if e.code != 404:
                            raise
                    recs, tombs = merge_docs(local, ltombs, remote, rtombs)
                    self.db.apply_sync(recs, tombs)
                    if canonical(recs, tombs) != canonical(remote, rtombs):
                        doc = {"schema": 1, "app": "assignment-manager",
                               "updated_ms": int(time.time() * 1000),
                               "assignments": [recs[u] for u in sorted(recs, key=lambda u: (recs[u].get("deadline", ""), u))],
                               "tombstones": [{"uid": u, "deleted_ms": tombs[u]} for u in sorted(tombs)]}
                        body = {"message": f"Sync from desktop ({len(recs)} assignments)",
                                "content": base64.b64encode(json.dumps(doc, indent=1, ensure_ascii=False)
                                                            .encode("utf-8")).decode()}
                        if sha:
                            body["sha"] = sha
                        try:
                            self._req("PUT", url, token, body)
                        except urllib.error.HTTPError as e:
                            if e.code in (409, 422) and attempt < 2:
                                continue  # someone else wrote first: merge again
                            raise
                    self.status = {"state": "ok", "error": None, "repo": repo,
                                   "last": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    return
                except urllib.error.HTTPError as e:
                    msg = {401: "GitHub token is invalid or expired",
                           403: "Token has no permission for this repository",
                           404: "Repository not found (check owner/name and token access)"}.get(
                        e.code, f"GitHub error {e.code}")
                    self.status = {"state": "error", "error": msg, "repo": repo,
                                   "last": self.status.get("last")}
                    return
                except Exception as e:
                    self.status = {"state": "error", "error": f"Offline or network error: {e}",
                                   "repo": repo, "last": self.status.get("last")}
                    return

    def run(self):
        time.sleep(1)
        while True:
            try:
                self.sync_once()
            except Exception as e:
                self.status = {"state": "error", "error": str(e), "last": self.status.get("last")}
            self.wake.wait(self.INTERVAL)
            self.wake.clear()
            time.sleep(0.8)  # debounce bursts of edits


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, db):
        self.db = db
        self.token = secrets.token_urlsafe(24)
        self.sync = None
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
                        "settings": public_settings(server.db.settings()),
                        "sync": server.sync.status if server.sync else {"state": "off"},
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
                    if parts == ["api", "sync"] and method == "POST":
                        if server.sync:
                            server.sync.wake.set()
                        return self._send(200, {"ok": True})
                    if server.sync and method in ("POST", "PUT", "DELETE"):
                        threading.Timer(0.3, server.sync.wake.set).start()
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
                        return self._send(200, public_settings(server.db.settings()))
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
                            "pywebview", "certifi"], check=True, stdout=log, stderr=log, timeout=900)
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
    server.sync = GitHubSync(db)
    server.sync.start()
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
