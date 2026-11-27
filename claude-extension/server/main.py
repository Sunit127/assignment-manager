#!/usr/bin/env python3
"""
Assignment Manager MCP server - lets Claude read and manage the assignments
stored by the Assignment Manager desktop app (~/.assignment_manager/assignments.db).

Zero dependencies: implements the MCP stdio JSON-RPC protocol with the
Python standard library only.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB_PATH = os.environ.get("ASSIGNMENT_DB") or os.path.join(
    os.path.expanduser("~"), ".assignment_manager", "assignments.db")
DATE_FMT = "%Y-%m-%d %H:%M"
DAY_FMT = "%Y-%m-%d"
STATUSES = ["Not Started", "In Progress", "Review", "Submitted", "Completed", "Cancelled"]
PRIORITIES = ["Low", "Medium", "High", "Urgent"]
PAYMENTS = ["Unpaid", "Partially Paid", "Paid"]
DONE = {"Submitted", "Completed", "Cancelled"}
FIELDS = ["title", "client", "client_contact", "subject", "module_code", "word_count",
          "assigned_date", "deadline", "priority", "status", "amount", "payment_status",
          "description", "notes"]

SERVER_INFO = {"name": "assignment-manager", "version": "1.0.0"}


# ----------------------------------------------------------------------------
# Database (same schema as the desktop app)
# ----------------------------------------------------------------------------
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, title TEXT NOT NULL,
            client TEXT, client_contact TEXT, subject TEXT, module_code TEXT,
            description TEXT, word_count TEXT, assigned_date TEXT, deadline TEXT NOT NULL,
            priority TEXT, status TEXT, amount TEXT, payment_status TEXT, notes TEXT,
            notified TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
    return conn


def parse_dt(text):
    text = (text or "").strip().replace("T", " ")
    for fmt in (DATE_FMT, "%Y-%m-%d %H:%M:%S", DAY_FMT):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(hour=23, minute=59) if fmt == DAY_FMT else dt
        except ValueError:
            pass
    return None


def time_left(dl, now):
    secs = int((dl - now).total_seconds())
    over = secs < 0
    secs = abs(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    txt = f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")
    return f"OVERDUE by {txt}" if over else f"{txt} left"


def row_dict(r, now=None):
    now = now or datetime.now()
    d = {k: r[k] for k in r.keys() if k != "notified"}
    dl = parse_dt(r["deadline"])
    if r["status"] in DONE:
        d["time_left"] = "done"
        d["urgency"] = "done"
    elif dl:
        d["time_left"] = time_left(dl, now)
        if dl < now:
            d["urgency"] = "overdue"
        elif dl.date() == now.date():
            d["urgency"] = "due today"
        elif dl - now <= timedelta(days=3):
            d["urgency"] = "due soon"
        else:
            d["urgency"] = "upcoming"
    return d


def find(conn, ident):
    ident = str(ident).strip()
    r = conn.execute("SELECT * FROM assignments WHERE code=? COLLATE NOCASE", (ident,)).fetchone()
    if not r and ident.isdigit():
        r = conn.execute("SELECT * FROM assignments WHERE id=?", (int(ident),)).fetchone()
    if not r:
        raise ValueError(f"No assignment found with ID '{ident}'")
    return r


def validate(data, partial=False):
    clean = {}
    for k, v in data.items():
        if k not in FIELDS or v is None:
            continue
        v = str(v).strip()
        if k == "deadline":
            dt = parse_dt(v)
            if not dt:
                raise ValueError("deadline must be 'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD')")
            v = dt.strftime(DATE_FMT)
        elif k == "assigned_date" and v and not parse_dt(v):
            raise ValueError("assigned_date must be 'YYYY-MM-DD'")
        elif k == "status" and v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        elif k == "priority" and v not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        elif k == "payment_status" and v not in PAYMENTS:
            raise ValueError(f"payment_status must be one of {PAYMENTS}")
        clean[k] = v
    if not partial:
        if not clean.get("title"):
            raise ValueError("title is required")
        if not clean.get("deadline"):
            raise ValueError("deadline is required")
    return clean


# ----------------------------------------------------------------------------
# Tool implementations
# ----------------------------------------------------------------------------
def t_list(args):
    now = datetime.now()
    status = args.get("status", "active")
    q = (args.get("search") or "").lower().strip()
    out = []
    with db() as conn:
        for r in conn.execute("SELECT * FROM assignments ORDER BY deadline"):
            d = row_dict(r, now)
            if status == "active" and r["status"] in DONE:
                continue
            if status == "overdue" and d.get("urgency") != "overdue":
                continue
            if status in STATUSES and r["status"] != status:
                continue
            if args.get("client") and (args["client"].lower() not in (r["client"] or "").lower()):
                continue
            if q and q not in " ".join(str(r[k] or "") for k in
                                       ("code", "title", "client", "subject", "module_code",
                                        "description", "notes")).lower():
                continue
            out.append(d)
    return {"count": len(out), "assignments": out}


def t_get(args):
    with db() as conn:
        return row_dict(find(conn, args["id"]))


def t_upcoming(args):
    days = int(args.get("days", 7))
    now = datetime.now()
    res = {"overdue": [], "due_within_days": []}
    with db() as conn:
        for r in conn.execute("SELECT * FROM assignments ORDER BY deadline"):
            if r["status"] in DONE:
                continue
            dl = parse_dt(r["deadline"])
            if not dl:
                continue
            if dl < now:
                res["overdue"].append(row_dict(r, now))
            elif dl - now <= timedelta(days=days):
                res["due_within_days"].append(row_dict(r, now))
    res["days"] = days
    res["next_deadline"] = res["due_within_days"][0] if res["due_within_days"] else None
    return res


def t_summary(_args):
    now = datetime.now()
    c = {"total": 0, "active": 0, "overdue": 0, "due_today": 0, "due_soon_3d": 0, "done": 0,
         "unpaid_or_partial": 0}
    nxt = None
    with db() as conn:
        for r in conn.execute("SELECT * FROM assignments ORDER BY deadline"):
            c["total"] += 1
            d = row_dict(r, now)
            if r["payment_status"] in ("Unpaid", "Partially Paid"):
                c["unpaid_or_partial"] += 1
            if r["status"] in DONE:
                c["done"] += 1
                continue
            c["active"] += 1
            u = d.get("urgency")
            if u == "overdue":
                c["overdue"] += 1
            elif u == "due today":
                c["due_today"] += 1
                c["due_soon_3d"] += 1
            elif u == "due soon":
                c["due_soon_3d"] += 1
            if u != "overdue" and nxt is None:
                nxt = d
    c["next_deadline"] = nxt
    c["now"] = now.strftime(DATE_FMT)
    return c


def t_add(args):
    data = validate(args)
    data.setdefault("priority", "Medium")
    data.setdefault("status", "Not Started")
    data.setdefault("payment_status", "Unpaid")
    data.setdefault("assigned_date", datetime.now().strftime(DAY_FMT))
    now = datetime.now().strftime(DATE_FMT)
    with db() as conn:
        cur = conn.execute(
            f"INSERT INTO assignments ({','.join(FIELDS)}, created_at, updated_at) "
            f"VALUES ({','.join('?' * len(FIELDS))}, ?, ?)",
            [data.get(f, "") for f in FIELDS] + [now, now])
        code = f"ASG-{datetime.now():%Y}-{cur.lastrowid:04d}"
        conn.execute("UPDATE assignments SET code=? WHERE id=?", (code, cur.lastrowid))
        return {"created": row_dict(find(conn, code))}


def t_update(args):
    ident = args.pop("id")
    data = validate(args, partial=True)
    if not data:
        raise ValueError("No fields to update")
    with db() as conn:
        r = find(conn, ident)
        sets = ", ".join(f"{k}=?" for k in data)
        vals = list(data.values())
        if "deadline" in data and data["deadline"] != r["deadline"]:
            sets += ", notified=''"  # restart reminders in the desktop app
        conn.execute(f"UPDATE assignments SET {sets}, updated_at=? WHERE id=?",
                     vals + [datetime.now().strftime(DATE_FMT), r["id"]])
        return {"updated": row_dict(find(conn, r["id"]))}


def t_delete(args):
    with db() as conn:
        r = find(conn, args["id"])
        conn.execute("DELETE FROM assignments WHERE id=?", (r["id"],))
        return {"deleted": r["code"], "title": r["title"]}


FIELD_SCHEMA = {
    "title": {"type": "string", "description": "Assignment title"},
    "client": {"type": "string", "description": "Client name"},
    "client_contact": {"type": "string", "description": "Client email or phone"},
    "subject": {"type": "string"},
    "module_code": {"type": "string", "description": "Module / course code, e.g. COMP1827"},
    "word_count": {"type": "string", "description": "Word count or number of pages"},
    "assigned_date": {"type": "string", "description": "YYYY-MM-DD"},
    "deadline": {"type": "string", "description": "YYYY-MM-DD HH:MM (24h, local time)"},
    "priority": {"type": "string", "enum": PRIORITIES},
    "status": {"type": "string", "enum": STATUSES},
    "amount": {"type": "string", "description": "Fee / price"},
    "payment_status": {"type": "string", "enum": PAYMENTS},
    "description": {"type": "string", "description": "Requirements / brief"},
    "notes": {"type": "string"},
}

TOOLS = [
    {"name": "get_summary",
     "description": "Dashboard summary: counts of overdue, due today, due soon, active, completed, "
                    "unpaid, plus the next (current) deadline.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}, "fn": t_summary},
    {"name": "get_upcoming_deadlines",
     "description": "List overdue assignments and those due within the next N days (default 7), "
                    "soonest first.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "Look-ahead window in days", "default": 7}}},
     "annotations": {"readOnlyHint": True}, "fn": t_upcoming},
    {"name": "list_assignments",
     "description": "List assignments sorted by deadline, with filters.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "description": "'active' (default), 'all', 'overdue', or a "
                                                     "specific status", "default": "active"},
         "client": {"type": "string", "description": "Filter by client name (partial match)"},
         "search": {"type": "string", "description": "Free-text search"}}},
     "annotations": {"readOnlyHint": True}, "fn": t_list},
    {"name": "get_assignment",
     "description": "Get every detail of one assignment by its ID (e.g. ASG-2026-0003).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                     "required": ["id"]},
     "annotations": {"readOnlyHint": True}, "fn": t_get},
    {"name": "add_assignment",
     "description": "Create a new assignment. An ID like ASG-2026-0007 is generated automatically.",
     "inputSchema": {"type": "object", "properties": FIELD_SCHEMA,
                     "required": ["title", "deadline"]},
     "fn": t_add},
    {"name": "update_assignment",
     "description": "Update any fields of an assignment (status, deadline, payment, notes, ...). "
                    "Only the fields you pass are changed.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string", "description": "Assignment ID"},
                                    **FIELD_SCHEMA},
                     "required": ["id"]},
     "fn": t_update},
    {"name": "delete_assignment",
     "description": "Permanently delete an assignment. Confirm with the user first.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                     "required": ["id"]},
     "annotations": {"destructiveHint": True}, "fn": t_delete},
]


# ----------------------------------------------------------------------------
# MCP stdio JSON-RPC loop
# ----------------------------------------------------------------------------
def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method, rid = req.get("method"), req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": "Manages the user's client assignments from the Assignment "
                                "Manager desktop app. Dates are local time, 'YYYY-MM-DD HH:MM'."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]}
    if method == "tools/call":
        tool = next((t for t in TOOLS if t["name"] == params.get("name")), None)
        if not tool:
            raise LookupError(f"Unknown tool {params.get('name')}")
        try:
            result = tool["fn"](dict(params.get("arguments") or {}))
            return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "structuredContent": result if isinstance(result, dict) else None}
        except Exception as e:  # tool errors are returned to the model, not as protocol errors
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    if rid is None:
        return None  # notification
    raise LookupError(f"Method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        rid = req.get("id")
        try:
            result = handle(req)
            if rid is not None:
                if result is not None and "structuredContent" in result and result["structuredContent"] is None:
                    del result["structuredContent"]
                send({"jsonrpc": "2.0", "id": rid, "result": result})
        except LookupError as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": str(e)}})
        except Exception as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
