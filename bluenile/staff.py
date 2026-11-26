"""The staff app (staff_app/), connected to the workshop database.

The staff app keeps its data as documents in collections (jobs, bookings,
parts, cars, stor, settings). Originally they lived only on one phone. Here
they live in the shop database, so every phone sees the same data:

    GET    /staff/                      the app (staff password required)
    GET    /staff/api/changes?since=N   documents changed after revision N
    PUT    /staff/api/doc/<coll>/<id>   save a document
    DELETE /staff/api/doc/<coll>/<id>   delete a document

staff_app/storage.js talks to these, giving the app the same calls it had.

Links to the rest of the system:
- Online bookings are written into the app's `bookings` (id "web-<appointment id>").
- Cancelling or no-showing one of those in the app frees the slot online.
- Bookings made in the app block those times for online booking.
- Tyres and wheels in the app's stock (`parts`) are offered on the website.
- The app's business details start from bluenile/shop_details.json.

Sign-in: one shared staff password (STAFF_PASSWORD, kept out of the repo).
Without it the staff app is switched off.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, session

from .brand import emblem_svg
from .tools import ShopConfig, setting

COLLECTIONS = {"parts", "jobs", "bookings", "cars", "stor", "settings"}
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_DOC_BYTES = 256 * 1024
APP_DIR = Path(__file__).resolve().parent.parent / "staff_app"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_changes (rev INTEGER PRIMARY KEY AUTOINCREMENT, collection TEXT, id TEXT);
CREATE TABLE IF NOT EXISTS app_docs (
    collection TEXT NOT NULL,
    id         TEXT NOT NULL,
    data       TEXT,             -- JSON, NULL when deleted
    rev        INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS app_docs_rev ON app_docs(rev);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Document store
# ---------------------------------------------------------------------------

def put_doc(conn: sqlite3.Connection, collection: str, doc_id: str, data: dict | None) -> int:
    """Save (or delete, with data=None) a document. Returns its new revision."""
    ensure_schema(conn)
    rev = conn.execute("INSERT INTO app_changes (collection, id) VALUES (?, ?)", (collection, doc_id)).lastrowid
    conn.execute(
        "INSERT INTO app_docs (collection, id, data, rev, updated_at) VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(collection, id) DO UPDATE SET data=excluded.data, rev=excluded.rev, updated_at=excluded.updated_at",
        (collection, doc_id, None if data is None else json.dumps(data, ensure_ascii=False), rev),
    )
    return rev


def get_doc(conn: sqlite3.Connection, collection: str, doc_id: str) -> dict | None:
    ensure_schema(conn)
    row = conn.execute("SELECT data FROM app_docs WHERE collection=? AND id=?", (collection, doc_id)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def docs(conn: sqlite3.Connection, collection: str) -> dict[str, dict]:
    ensure_schema(conn)
    rows = conn.execute("SELECT id, data FROM app_docs WHERE collection=? AND data IS NOT NULL", (collection,))
    return {r[0]: json.loads(r[1]) for r in rows}


def changes_since(conn: sqlite3.Connection, since: int) -> dict:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT collection, id, data, rev FROM app_docs WHERE rev > ? ORDER BY rev LIMIT 5000", (since,)
    ).fetchall()
    latest = conn.execute("SELECT COALESCE(MAX(rev), 0) FROM app_changes").fetchone()[0]
    # rev: where the client should ask from next. If the batch was cut short, rev < latest and
    # the client asks again straight away.
    return {
        "rev": rows[-1][3] if rows else latest,
        "latest": latest,
        "docs": [{"c": r[0], "id": r[1], "data": json.loads(r[2]) if r[2] else None} for r in rows],
    }


def seed_settings(conn: sqlite3.Connection, cfg: ShopConfig) -> None:
    """Fill the app's business details from shop_details.json the first time."""
    if get_doc(conn, "settings", "business") is None:
        put_doc(conn, "settings", "business", {
            "name": cfg.name, "abn": cfg.abn.replace(" ", ""), "phone": cfg.phone,
            "address": cfg.address, "email": cfg.email, "pay": "",
        })
        conn.commit()


# ---------------------------------------------------------------------------
# Links to bookings and the website
# ---------------------------------------------------------------------------

def staff_bookings_on(conn: sqlite3.Connection, day: str) -> list[dict]:
    """Bookings made in the staff app (not online ones, which are appointments already) on a date."""
    return [b for i, b in docs(conn, "bookings").items()
            if not i.startswith("web-") and b.get("date") == day and b.get("status", "booked") == "booked"]


def staff_busy_count(conn: sqlite3.Connection, start: datetime, end: datetime, booking_hours: float) -> int:
    """How many staff-app bookings overlap [start, end). Each is assumed to take booking_hours."""
    n = 0
    for b in staff_bookings_on(conn, start.date().isoformat()):
        m = re.match(r"^(\d\d):(\d\d)", b.get("time") or "")
        if not m:
            continue  # no time given: can't tell when it is
        b_start = start.replace(hour=int(m[1]), minute=int(m[2]))
        if b_start < end and b_start + timedelta(hours=booking_hours) > start:
            n += 1
    return n


def write_online_booking(conn: sqlite3.Connection, appt: dict, customer: dict, vehicle: dict,
                         services: list[str], quotes: list[str], notes: str) -> None:
    """Show an online booking in the staff app's Bookings."""
    start = datetime.fromisoformat(appt["start_at"])
    vehicle_text = " ".join(str(x) for x in (vehicle.get("make"), vehicle.get("model"), vehicle.get("year")) if x)
    put_doc(conn, "bookings", f"web-{appt['id']}", {
        "name": customer["name"], "phone": customer.get("phone") or "", "email": customer.get("email") or "",
        "rego": (vehicle.get("plate") or "").upper(), "make": vehicle.get("make") or "",
        "model": vehicle.get("model") or "", "year": str(vehicle.get("year") or ""), "vehicle": vehicle_text,
        "service": "Online: " + ", ".join(services + quotes),
        "date": start.date().isoformat(), "time": start.strftime("%H:%M"), "status": "booked",
        "source": "online", "appointment_id": appt["id"], "notes": notes,
        "quote_needed": quotes,
    })


def sync_appointment_status(conn: sqlite3.Connection, doc_id: str, data: dict | None) -> None:
    """When staff cancel / no-show an online booking in the app, free the slot."""
    if not doc_id.startswith("web-"):
        return
    try:
        appt_id = int(doc_id[4:])
    except ValueError:
        return
    status = (data or {}).get("status", "cancelled")
    new = {"booked": "scheduled", "cancelled": "cancelled", "noshow": "cancelled"}.get(status, "scheduled")
    conn.execute("UPDATE appointments SET status=? WHERE id=?", (new, appt_id))


def stock_prices_ex_gst() -> bool:
    return setting("STAFF_PRICES_EX_GST", "1") == "1"


def public_stock(conn: sqlite3.Connection, cfg: ShopConfig) -> dict[str, dict]:
    """Tyres and wheels in stock, priced for the public (GST included when the shop's prices are)."""
    out = {}
    add_gst = stock_prices_ex_gst() and cfg.tax_inclusive
    for sku, p in docs(conn, "parts").items():
        if p.get("category") not in ("tyre", "wheel"):
            continue
        try:
            qty = int(p.get("qty") or 0)
            price = float(p.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        cents = round(price * 100 * (1 + cfg.tax_rate)) if add_gst else round(price * 100)
        out[sku] = {
            "sku": sku, "kind": p["category"], "brand": p.get("brand", ""), "model": p.get("model", ""),
            "size": p.get("size", ""), "pcd": p.get("pcd", ""), "material": p.get("material", ""),
            "price_cents": cents, "available": qty,
            "name": p.get("name") or " ".join(x for x in (p.get("brand"), p.get("model"), p.get("size")) if x),
        }
    return out


def tyre_size_key(size: str) -> str:
    """'225/45R17', '225/45 R17', '225 45 17' -> '2254517'."""
    return re.sub(r"[^0-9]", "", size or "")


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><html lang="en-AU"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Staff sign in</title>
<meta name="robots" content="noindex">
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0d0f14; color: #f5f7fa;
  font: 16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; padding: 16px; }}
form {{ width: min(360px, 100%); background: #151a24; border: 1px solid #262f40; border-radius: 18px; padding: 28px; text-align: center; }}
h1 {{ font-size: 1.2rem; margin: 14px 0 4px; }} p {{ color: #9aa6b8; margin: 0 0 18px; font-size: .92rem; }}
input {{ width: 100%; box-sizing: border-box; padding: 12px 14px; border-radius: 10px; border: 1px solid #262f40; background: #0d0f14; color: inherit; font: inherit; }}
button {{ width: 100%; margin-top: 12px; padding: 12px; border: 0; border-radius: 10px; background: #ffb81c; color: #0d0f14; font: inherit; font-weight: 700; cursor: pointer; }}
.err {{ color: #ff8a80; margin: 12px 0 0; font-weight: 600; }}
</style></head><body>
<form method="post" action="/staff/login">{emblem}<h1>Blue Nile staff</h1><p>Sign in to jobs, bookings and stock.</p>
<label for="pw" style="position:absolute;left:-9999px">Staff password</label>
<input id="pw" type="password" name="password" autocomplete="current-password" placeholder="Staff password" autofocus required>
<button type="submit">Sign in</button>{error}</form></body></html>"""


class _Limiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def _secret_key(db_path: Path) -> str:
    key = os.environ.get("STAFF_SECRET_KEY")
    if key:
        return key
    f = db_path.with_name(".staff_secret")
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


def register_staff_app(app: Flask, conn_factory: Callable[[], sqlite3.Connection], cfg_factory: Callable[[], ShopConfig],
                       db_path: Path, password: str | None = None, app_dir: Path = APP_DIR,
                       secure_cookies: bool = True) -> None:
    password = password if password is not None else os.environ.get("STAFF_PASSWORD", "")
    app.secret_key = app.secret_key or _secret_key(db_path)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=secure_cookies, PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    login_limiter = _Limiter(10, 900)

    def enabled() -> bool:
        return bool(password)

    def signed_in() -> bool:
        return enabled() and session.get("staff") is True

    def need_api_auth():
        if not signed_in():
            abort(401)
        if request.method in ("PUT", "DELETE") and request.headers.get("X-Staff") != "1":
            abort(403)  # blocks cross-site form posts; our storage.js always sends it

    @app.route("/staff/login", methods=["GET", "POST"])
    def staff_login():
        if not enabled():
            return Response("The staff app is switched off. Set STAFF_PASSWORD on the server to turn it on.",
                            503, mimetype="text/plain")
        error = ""
        if request.method == "POST":
            if not login_limiter.allow(request.remote_addr or "?"):
                error = '<p class="err">Too many tries. Wait 15 minutes.</p>'
            elif hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                session.clear()
                session.permanent = True
                session["staff"] = True
                return redirect("/staff/")
            else:
                error = '<p class="err">That password isn\'t right.</p>'
        resp = Response(LOGIN_PAGE.format(emblem=emblem_svg(72), error=error), mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/staff/logout")
    def staff_logout():
        session.clear()
        return redirect("/staff/login")

    @app.get("/staff")
    @app.get("/staff/")
    def staff_index():
        if not signed_in():
            return redirect("/staff/login")
        resp = send_from_directory(app_dir, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/staff/<path:name>")
    def staff_files(name):
        if name.startswith("api/"):
            abort(404)
        # icons, manifest and the service worker are public so "Add to Home Screen" works;
        # the app page and all data need a signed-in session.
        if name == "index.html":
            if not signed_in():
                return redirect("/staff/login")
        elif name not in {"storage.js", "sw.js", "manifest.webmanifest"} and not name.startswith("icons/"):
            abort(404)
        resp = send_from_directory(app_dir, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/staff/api/changes")
    def staff_changes():
        need_api_auth()
        try:
            since = int(request.args.get("since", "0"))
        except ValueError:
            since = 0
        conn = conn_factory()
        try:
            seed_settings(conn, cfg_factory())
            resp = jsonify(changes_since(conn, since))
        finally:
            conn.close()
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/staff/api/doc/<collection>/<doc_id>", methods=["PUT", "DELETE"])
    def staff_doc(collection, doc_id):
        need_api_auth()
        if collection not in COLLECTIONS or not ID_RE.match(doc_id):
            abort(400)
        data = None
        if request.method == "PUT":
            if (request.content_length or 0) > MAX_DOC_BYTES:
                abort(413)
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                abort(400)
        conn = conn_factory()
        try:
            rev = put_doc(conn, collection, doc_id, data)
            if collection == "bookings":
                sync_appointment_status(conn, doc_id, data)
            conn.commit()
        finally:
            conn.close()
        return jsonify(rev=rev)
