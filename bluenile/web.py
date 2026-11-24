"""Online booking API, and the server that hosts the public website.

    GET  /                      the website (website/index.html)
    GET  /api/availability      ?date=YYYY-MM-DD&hours=N -> free start times
    POST /api/bookings          JSON booking from the website's checkout

A booking goes straight into the workshop database: the customer is matched
by phone number (or created), the vehicle by rego (or added), a bay is booked,
and a work order is opened with every fixed-price item already on it. Items
without a fixed price (ranges, unconfirmed prices, tyre requests) are listed in
the work order notes as "Quote needed" so staff call the customer.

Prices are always re-read from the price list here; the browser only sends
service names and quantities.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory

from .tools import ShopConfig, ShopTools, ToolError

MAX_QTY = 4
MAX_HOURS = 4
BOOKING_HORIZON_DAYS = 60
MIN_NOTICE = timedelta(hours=1)
RATE_LIMIT = (5, 3600)  # bookings per IP per window (seconds)


class BookingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _plate(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _text(payload: dict, key: str, max_len: int, required: bool = False) -> str:
    value = payload.get(key) or ""
    if not isinstance(value, str):
        raise BookingError(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise BookingError(f"{key} is required")
    if len(value) > max_len:
        raise BookingError(f"{key} is too long")
    return value


def fixed_price_cents(item: dict) -> int | None:
    """The single price for an item, or None when it needs a quote (range, 'from', unconfirmed)."""
    lo, hi = item.get("price_min"), item.get("price_max")
    if item.get("confirmed") and lo is not None and lo == hi:
        return round(lo * 100)
    return None


def load_catalog(cfg: ShopConfig) -> dict:
    return json.loads(Path(cfg.price_list).read_text(encoding="utf-8"))


def parse_booking(payload: dict, catalog: dict, now: datetime) -> dict:
    """Validate a checkout payload. Returns a normalised booking or raises BookingError."""
    if not isinstance(payload, dict):
        raise BookingError("Invalid booking")
    if payload.get("company"):  # honeypot field, hidden from people
        raise BookingError("Invalid booking")

    services = {s["name"]: s for s in catalog["services"]}
    items = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict) or raw.get("name") not in services:
            raise BookingError("Unknown service in booking")
        qty = raw.get("qty", 1)
        if not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            raise BookingError("Quantity must be between 1 and 4")
        items.append({"service": services[raw["name"]], "qty": qty})

    tyres = None
    if payload.get("tyres"):
        t = payload["tyres"]
        try:
            tyres = {k: int(t[k]) for k in ("width", "profile", "rim", "qty")}
        except (KeyError, TypeError, ValueError) as e:
            raise BookingError("Tyre size is incomplete") from e
        if not (125 <= tyres["width"] <= 355 and 25 <= tyres["profile"] <= 85
                and 12 <= tyres["rim"] <= 24 and 1 <= tyres["qty"] <= 6):
            raise BookingError("Tyre size doesn't look right")
    if not items and not tyres:
        raise BookingError("Add at least one service")

    vehicle = payload.get("vehicle") or {}
    customer = payload.get("customer") or {}
    v = {
        "rego": _plate(_text(vehicle, "rego", 12)),
        "make": _text(vehicle, "make", 40, required=True),
        "model": _text(vehicle, "model", 40, required=True),
        "year": None,
    }
    if vehicle.get("year"):
        try:
            v["year"] = int(vehicle["year"])
        except (TypeError, ValueError) as e:
            raise BookingError("Year must be a number") from e
        if not 1950 <= v["year"] <= now.year + 1:
            raise BookingError("Year doesn't look right")
    c = {
        "name": _text(customer, "name", 80, required=True),
        "phone": _text(customer, "phone", 25, required=True),
        "email": _text(customer, "email", 120),
    }
    if not 8 <= len(_digits(c["phone"])) <= 15:
        raise BookingError("Please enter a valid phone number")
    if c["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", c["email"]):
        raise BookingError("Please enter a valid email")

    try:
        start = datetime.fromisoformat(_text(payload, "slot", 16, required=True))
    except ValueError as e:
        raise BookingError("Pick a time for your booking") from e
    if start < now + MIN_NOTICE:
        raise BookingError("That time has passed; please pick another")
    if start > now + timedelta(days=BOOKING_HORIZON_DAYS):
        raise BookingError("Bookings open up to 60 days ahead")

    hours = max([i["service"].get("hours", 1) for i in items] + [1])
    return {"items": items, "tyres": tyres, "vehicle": v, "customer": c, "start": start,
            "hours": min(hours, MAX_HOURS), "notes": _text(payload, "notes", 1000)}


def place_booking(tools: ShopTools, booking: dict) -> dict:
    """Write a validated booking into the workshop database."""
    c, v = booking["customer"], booking["vehicle"]
    phone = _digits(c["phone"])
    match = next((x for x in tools.find_customer(phone) if _digits(x.get("phone") or "") == phone), None)
    if match:
        customer = tools.save_customer(name=match["name"], email=c["email"] or None, customer_id=match["id"])
        vehicles = match["vehicles"]
    else:
        customer = tools.save_customer(name=c["name"], phone=c["phone"], email=c["email"] or None,
                                       notes="Created by online booking")
        vehicles = []
    vehicle = next((x for x in vehicles if v["rego"] and _plate(x.get("plate") or "") == v["rego"]), None)
    if vehicle is None:
        vehicle = tools.add_vehicle(customer_id=customer["id"], make=v["make"], model=v["model"],
                                    year=v["year"], plate=v["rego"] or None)

    priced, quotes = [], []
    for it in booking["items"]:
        s, qty = it["service"], it["qty"]
        cents = fixed_price_cents(s)
        label = s["name"] + (f" x{qty}" if qty > 1 else "")
        (priced if cents is not None else quotes).append((s, qty, cents, label))
    tyre_label = None
    if booking["tyres"]:
        t = booking["tyres"]
        tyre_label = f"{t['qty']} x tyres {t['width']}/{t['profile']}R{t['rim']}"
        quotes.append((None, t["qty"], None, tyre_label))

    summary = "Online booking: " + ", ".join(label for *_, label in priced + quotes)
    try:
        appt = tools.book_appointment(customer_id=customer["id"], vehicle_id=vehicle["id"],
                                      start=booking["start"].isoformat(timespec="minutes"),
                                      duration_hours=booking["hours"], service=summary[:500])
    except ToolError as e:
        raise BookingError(str(e), status=409) from e

    complaint = booking["notes"] or summary
    wo = tools.create_work_order(customer_id=customer["id"], vehicle_id=vehicle["id"],
                                 complaint=complaint, appointment_id=appt["id"])
    for s, qty, cents, _ in priced:
        kind = "labor" if s.get("unit") == "per hour" else "fee"
        tools.add_work_order_line(work_order_id=wo["id"], kind=kind, description=s["name"],
                                  quantity=qty, unit_price=cents / 100)
    if quotes:
        tools.update_work_order(work_order_id=wo["id"], notes="Booked online. Quote needed, call the customer: "
                                + "; ".join(label for *_, label in quotes))
    else:
        tools.update_work_order(work_order_id=wo["id"], notes="Booked online.")
    tools.conn.commit()

    total = sum(cents * qty for _, qty, cents, _ in priced)
    return {
        "booking_id": appt["id"],
        "work_order_id": wo["id"],
        "start": appt["start_at"],
        "end": appt["end_at"],
        "total_cents": total,
        "priced_items": [{"name": s["name"], "qty": qty, "unit_cents": cents} for s, qty, cents, _ in priced],
        "quote_items": [label for *_, label in quotes],
    }


class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


def create_web_app(tools_factory: Callable[[], ShopTools], site_dir: Path = Path("website"),
                   now: Callable[[], datetime] = datetime.now) -> Flask:
    app = Flask(__name__)
    limiter = RateLimiter(*RATE_LIMIT)
    site_dir = site_dir.resolve()

    def get_tools() -> ShopTools:
        if "tools" not in g:
            g.tools = tools_factory()
        return g.tools

    @app.teardown_appcontext
    def close_tools(exc):
        tools = g.pop("tools", None)
        if tools is not None:
            tools.conn.close()

    @app.get("/")
    def index():
        if not (site_dir / "index.html").exists():
            abort(404, "Run build_website.py first")
        return send_from_directory(site_dir, "index.html")

    @app.get("/api/availability")
    def availability():
        tools = get_tools()
        try:
            day = date.fromisoformat(request.args.get("date", ""))
            hours = float(request.args.get("hours", "1"))
        except ValueError:
            return jsonify(error="Use date=YYYY-MM-DD"), 400
        today = now()
        if day < today.date() or day > today.date() + timedelta(days=BOOKING_HORIZON_DAYS):
            return jsonify(date=day.isoformat(), open=False, free_slots=[])
        result = tools.check_availability(date=day.isoformat(), duration_hours=min(max(hours, 1), MAX_HOURS))
        earliest = today + MIN_NOTICE
        result["free_slots"] = [s for s in result["free_slots"] if datetime.fromisoformat(s) >= earliest]
        return jsonify(result)

    @app.post("/api/bookings")
    def bookings():
        if not limiter.allow(request.remote_addr or "?"):
            return jsonify(error="Too many bookings from this connection. Please call us."), 429
        tools = get_tools()
        try:
            booking = parse_booking(request.get_json(silent=True), load_catalog(tools.cfg), now())
            result = place_booking(tools, booking)
        except BookingError as e:
            tools.conn.rollback()
            return jsonify(error=str(e)), e.status
        return jsonify(result), 201

    @app.after_request
    def no_cache_api(resp: Response) -> Response:
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
