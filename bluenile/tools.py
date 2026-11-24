"""Shop tools the agent calls. They run on *your* machine (Managed Agents
"custom tools"), so customer and billing data never leave the local
database except as the results the agent asks for.

Each tool is a (schema, handler) pair. `ShopTools.run(name, input)` executes
one and returns a JSON string; it raises `ToolError` for bad input, which the
runner sends back to the agent as an error result.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .db import row_to_dict


class ToolError(Exception):
    """Input the agent should correct (unknown id, bad date, ...)."""


@dataclass
class ShopConfig:
    name: str = os.environ.get("SHOP_NAME", "Bluenile Auto Repair")
    address: str = os.environ.get("SHOP_ADDRESS", "")
    phone: str = os.environ.get("SHOP_PHONE", "")
    abn: str = os.environ.get("SHOP_ABN", "")
    labor_rate_cents: int = int(float(os.environ.get("SHOP_LABOR_RATE", "130")) * 100)
    # Default: Australian GST, 10%, already included in every price (parts and labour).
    # For tax added on top instead, set SHOP_TAX_INCLUSIVE=0.
    tax_name: str = os.environ.get("SHOP_TAX_NAME", "GST")
    tax_rate: float = float(os.environ.get("SHOP_TAX_RATE", "0.10"))
    tax_inclusive: bool = os.environ.get("SHOP_TAX_INCLUSIVE", "1") == "1"
    tax_labor: bool = os.environ.get("SHOP_TAX_LABOR", "1") == "1"
    open_hour: int = int(os.environ.get("SHOP_OPEN_HOUR", "8"))
    close_hour: int = int(os.environ.get("SHOP_CLOSE_HOUR", "17"))
    bays: int = int(os.environ.get("SHOP_BAYS", "3"))
    # Monday=0 ... Sunday=6
    open_days: tuple[int, ...] = field(default=(0, 1, 2, 3, 4, 5))
    invoice_dir: Path = Path(os.environ.get("SHOP_INVOICE_DIR", "invoices"))
    price_list: Path = Path(os.environ.get("SHOP_PRICE_LIST", Path(__file__).with_name("price_list.json")))


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"Invalid date/time {value!r}; use ISO format like 2026-12-01T09:00") from e


# Words callers use -> words on the price list.
_SYNONYMS = {
    "tire": "tyre", "tires": "tyres", "aircon": "a/c", "ac": "a/c", "air-con": "a/c",
    "conditioning": "a/c", "roadworthy": "rwc", "rego": "rwc", "oil": "standard",
    "logbook": "standard", "bulb": "globe", "headlight": "headlight", "brakes": "brake",
    "flat": "puncture", "alignment": "alignment", "wiper": "wipers", "gearbox": "transmission",
    "trans": "transmission", "diagnosis": "diagnostic", "labor": "labour", "radiator": "coolant",
    "filter": "filter", "filters": "filter", "inspection": "inspection",
}


_STOPWORDS = set("""a an and the for of to in on at is are be do does did you your my me i we can could
would how much what whats what's cost costs price prices pricing quote charge get need new please change
with car cars vehicle just about some""".split())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:/[a-z0-9]+)?", text.lower())


def _quote(item: dict) -> str:
    lo, hi, unit = item.get("price_min"), item.get("price_max"), item.get("unit")
    if lo is None and hi is None:
        return "no price on the list"
    if hi is None:
        text = f"from ${lo:g}"
    elif lo is None or lo == hi:
        text = f"${hi:g}"
    else:
        text = f"${lo:g} to ${hi:g}"
    return f"{text} {unit}" if unit else text


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}


class ShopTools:
    def __init__(self, conn: sqlite3.Connection, config: ShopConfig | None = None):
        self.conn = conn
        self.cfg = config or ShopConfig()
        self._tools: dict[str, tuple[dict, Callable[..., Any]]] = {}
        self._register_all()

    # ------------------------------------------------------------------ API
    def definitions(self) -> list[dict]:
        """Custom-tool definitions for the Managed Agents `tools` array."""
        return [
            {"type": "custom", "name": name, "description": schema["description"], "input_schema": schema["input_schema"]}
            for name, (schema, _) in self._tools.items()
        ]

    def run(self, name: str, tool_input: dict) -> str:
        if name not in self._tools:
            raise ToolError(f"Unknown tool {name!r}")
        _, fn = self._tools[name]
        try:
            result = fn(**tool_input)
        except TypeError as e:  # missing/unexpected arguments
            raise ToolError(f"Bad arguments for {name}: {e}") from e
        self.conn.commit()
        return json.dumps(result, default=str)

    # ------------------------------------------------------------ helpers
    def _add(self, name: str, description: str, input_schema: dict, fn: Callable[..., Any]) -> None:
        self._tools[name] = ({"description": description, "input_schema": input_schema}, fn)

    def _one(self, sql: str, *args) -> dict | None:
        return row_to_dict(self.conn.execute(sql, args).fetchone())

    def _all(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _require(self, table: str, row_id: int) -> dict:
        row = self._one(f"SELECT * FROM {table} WHERE id = ?", row_id)
        if row is None:
            raise ToolError(f"No {table[:-1].replace('_', ' ')} with id {row_id}")
        return row

    # -------------------------------------------------------------- tools
    def _register_all(self) -> None:
        self._add(
            "find_customer",
            "Search customers by name, phone, email, license plate or VIN (partial match). "
            "Returns matching customers with their vehicles. Always search before creating a customer.",
            _obj({"query": S}, ["query"]),
            self.find_customer,
        )
        self._add(
            "save_customer",
            "Create a customer, or update one when customer_id is given.",
            _obj({"name": S, "phone": S, "email": S, "notes": S, "customer_id": I}, ["name"]),
            self.save_customer,
        )
        self._add(
            "add_vehicle",
            "Add a vehicle to a customer.",
            _obj({"customer_id": I, "year": I, "make": S, "model": S, "vin": S, "plate": S, "mileage": I},
                 ["customer_id", "make", "model"]),
            self.add_vehicle,
        )
        self._add(
            "log_call",
            "Record a phone call: who called, why (intent), a short summary and any follow-up needed. "
            "Log every call before ending it.",
            _obj({
                "intent": {"type": "string", "enum": ["booking", "status_check", "quote", "billing", "complaint", "other"]},
                "summary": S, "caller_name": S, "phone": S, "customer_id": I, "follow_up": S,
            }, ["intent", "summary"]),
            self.log_call,
        )
        self._add(
            "list_calls",
            "List logged calls, newest first. Set only_follow_ups to see calls still needing a callback.",
            _obj({"only_follow_ups": {"type": "boolean"}, "limit": I}, []),
            self.list_calls,
        )
        self._add(
            "check_availability",
            "List free appointment start times on a date (YYYY-MM-DD) for a job of duration_hours.",
            _obj({"date": S, "duration_hours": N}, ["date"]),
            self.check_availability,
        )
        self._add(
            "book_appointment",
            "Book an appointment. start is ISO local time like 2026-12-01T09:00. "
            "Check availability first.",
            _obj({"customer_id": I, "vehicle_id": I, "start": S, "duration_hours": N, "service": S},
                 ["customer_id", "start", "service"]),
            self.book_appointment,
        )
        self._add(
            "cancel_appointment",
            "Cancel an appointment by id.",
            _obj({"appointment_id": I}, ["appointment_id"]),
            self.cancel_appointment,
        )
        self._add(
            "list_appointments",
            "List scheduled appointments on a date (YYYY-MM-DD), or for a customer.",
            _obj({"date": S, "customer_id": I}, []),
            self.list_appointments,
        )
        self._add(
            "create_work_order",
            "Open a work order (repair order) for a customer's vehicle with the customer's complaint.",
            _obj({"customer_id": I, "vehicle_id": I, "complaint": S, "appointment_id": I},
                 ["customer_id", "vehicle_id", "complaint"]),
            self.create_work_order,
        )
        self._add(
            "add_work_order_line",
            "Add a labor, part or fee line to an open work order. For labor, quantity is hours and "
            "unit_price defaults to the shop labor rate. unit_price is in dollars.",
            _obj({
                "work_order_id": I,
                "kind": {"type": "string", "enum": ["labor", "part", "fee"]},
                "description": S, "quantity": N, "unit_price": N,
            }, ["work_order_id", "kind", "description", "quantity"]),
            self.add_work_order_line,
        )
        self._add(
            "update_work_order",
            "Change a work order's status and/or append technician notes.",
            _obj({
                "work_order_id": I,
                "status": {"type": "string", "enum": ["open", "waiting_parts", "waiting_approval", "in_progress", "ready", "closed"]},
                "notes": S,
            }, ["work_order_id"]),
            self.update_work_order,
        )
        self._add(
            "get_work_order",
            "Get a work order with its vehicle, customer, lines and running totals.",
            _obj({"work_order_id": I}, ["work_order_id"]),
            self.get_work_order,
        )
        self._add(
            "list_work_orders",
            "List work orders, optionally filtered by status or customer.",
            _obj({"status": S, "customer_id": I}, []),
            self.list_work_orders,
        )
        self._add(
            "create_invoice",
            "Create the invoice for a work order from its lines, compute tax and totals, and write a "
            "printable HTML invoice. A work order can be invoiced once; this also marks it ready.",
            _obj({"work_order_id": I}, ["work_order_id"]),
            self.create_invoice,
        )
        self._add(
            "record_payment",
            "Record a payment against an invoice. amount is in dollars.",
            _obj({"invoice_id": I, "amount": N,
                  "method": {"type": "string", "enum": ["cash", "card", "check", "other"]}},
                 ["invoice_id", "amount", "method"]),
            self.record_payment,
        )
        self._add(
            "get_invoice",
            "Get an invoice with its payments and balance due.",
            _obj({"invoice_id": I}, ["invoice_id"]),
            self.get_invoice,
        )
        self._add(
            "lookup_prices",
            "Search the shop's service price list (services, flushes, filters, wipers, etc.). "
            "Leave query empty to get the whole list. Each result has a quote and a confirmed "
            "flag; unconfirmed or missing prices must be checked by staff before you quote them as final.",
            _obj({"query": S}, []),
            self.lookup_prices,
        )
        self._add(
            "shop_info",
            "Shop name, hours, labor rate and tax settings.",
            _obj({}, []),
            self.shop_info,
        )

    # Customers & vehicles ------------------------------------------------
    def find_customer(self, query: str) -> list[dict]:
        q = f"%{query.strip()}%"
        digits = "".join(ch for ch in query if ch.isdigit())
        rows = self._all(
            """SELECT DISTINCT c.* FROM customers c LEFT JOIN vehicles v ON v.customer_id = c.id
               WHERE c.name LIKE ? OR c.email LIKE ? OR v.plate LIKE ? OR v.vin LIKE ?
                  OR (? != '' AND replace(replace(replace(replace(c.phone,'-',''),' ',''),'(',''),')','') LIKE ?)
               ORDER BY c.name LIMIT 20""",
            q, q, q, q, digits, f"%{digits}%",
        )
        for c in rows:
            c["vehicles"] = self._all("SELECT * FROM vehicles WHERE customer_id = ?", c["id"])
        return rows

    def save_customer(self, name: str, phone: str | None = None, email: str | None = None,
                      notes: str | None = None, customer_id: int | None = None) -> dict:
        if customer_id is not None:
            self._require("customers", customer_id)
            self.conn.execute(
                "UPDATE customers SET name=?, phone=COALESCE(?, phone), email=COALESCE(?, email), "
                "notes=COALESCE(?, notes) WHERE id=?",
                (name, phone, email, notes, customer_id),
            )
        else:
            cur = self.conn.execute(
                "INSERT INTO customers (name, phone, email, notes) VALUES (?, ?, ?, ?)",
                (name, phone, email, notes),
            )
            customer_id = cur.lastrowid
        return self._one("SELECT * FROM customers WHERE id = ?", customer_id)

    def add_vehicle(self, customer_id: int, make: str, model: str, year: int | None = None,
                    vin: str | None = None, plate: str | None = None, mileage: int | None = None) -> dict:
        self._require("customers", customer_id)
        cur = self.conn.execute(
            "INSERT INTO vehicles (customer_id, year, make, model, vin, plate, mileage) VALUES (?,?,?,?,?,?,?)",
            (customer_id, year, make, model, vin, plate, mileage),
        )
        return self._one("SELECT * FROM vehicles WHERE id = ?", cur.lastrowid)

    # Calls ---------------------------------------------------------------
    def log_call(self, intent: str, summary: str, caller_name: str | None = None, phone: str | None = None,
                 customer_id: int | None = None, follow_up: str | None = None) -> dict:
        if customer_id is not None:
            self._require("customers", customer_id)
        cur = self.conn.execute(
            "INSERT INTO calls (customer_id, caller_name, phone, intent, summary, follow_up) VALUES (?,?,?,?,?,?)",
            (customer_id, caller_name, phone, intent, summary, follow_up),
        )
        return self._one("SELECT * FROM calls WHERE id = ?", cur.lastrowid)

    def list_calls(self, only_follow_ups: bool = False, limit: int = 20) -> list[dict]:
        where = "WHERE follow_up IS NOT NULL AND follow_up != ''" if only_follow_ups else ""
        return self._all(f"SELECT * FROM calls {where} ORDER BY id DESC LIMIT ?", limit)

    # Scheduling ----------------------------------------------------------
    def _busy_count(self, start: datetime, end: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE status='scheduled' AND start_at < ? AND end_at > ?",
            (end.isoformat(timespec="minutes"), start.isoformat(timespec="minutes")),
        ).fetchone()
        return row[0]

    def check_availability(self, date: str, duration_hours: float = 1) -> dict:
        day = _parse_dt(date)
        if day.weekday() not in self.cfg.open_days:
            return {"date": date, "open": False, "free_slots": []}
        dur = timedelta(hours=duration_hours)
        slots = []
        t = day.replace(hour=self.cfg.open_hour, minute=0)
        close = day.replace(hour=self.cfg.close_hour, minute=0)
        while t + dur <= close:
            if self._busy_count(t, t + dur) < self.cfg.bays:
                slots.append(t.strftime("%Y-%m-%dT%H:%M"))
            t += timedelta(hours=1)
        return {"date": date, "open": True, "duration_hours": duration_hours, "free_slots": slots}

    def book_appointment(self, customer_id: int, start: str, service: str,
                         vehicle_id: int | None = None, duration_hours: float = 1) -> dict:
        self._require("customers", customer_id)
        if vehicle_id is not None:
            v = self._require("vehicles", vehicle_id)
            if v["customer_id"] != customer_id:
                raise ToolError(f"Vehicle {vehicle_id} does not belong to customer {customer_id}")
        s = _parse_dt(start)
        e = s + timedelta(hours=duration_hours)
        if s.weekday() not in self.cfg.open_days:
            raise ToolError("The shop is closed that day")
        if s.hour < self.cfg.open_hour or e > s.replace(hour=self.cfg.close_hour, minute=0):
            raise ToolError(f"Outside shop hours ({self.cfg.open_hour}:00-{self.cfg.close_hour}:00)")
        if self._busy_count(s, e) >= self.cfg.bays:
            raise ToolError("No free bay at that time; check availability for another slot")
        cur = self.conn.execute(
            "INSERT INTO appointments (customer_id, vehicle_id, start_at, end_at, service) VALUES (?,?,?,?,?)",
            (customer_id, vehicle_id, s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes"), service),
        )
        return self._one("SELECT * FROM appointments WHERE id = ?", cur.lastrowid)

    def cancel_appointment(self, appointment_id: int) -> dict:
        self._require("appointments", appointment_id)
        self.conn.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appointment_id,))
        return self._one("SELECT * FROM appointments WHERE id = ?", appointment_id)

    def list_appointments(self, date: str | None = None, customer_id: int | None = None) -> list[dict]:
        sql = ("SELECT a.*, c.name AS customer_name, v.year, v.make, v.model FROM appointments a "
               "JOIN customers c ON c.id = a.customer_id LEFT JOIN vehicles v ON v.id = a.vehicle_id "
               "WHERE a.status = 'scheduled'")
        args: list[Any] = []
        if date:
            sql += " AND substr(a.start_at, 1, 10) = ?"
            args.append(date[:10])
        if customer_id is not None:
            sql += " AND a.customer_id = ?"
            args.append(customer_id)
        return self._all(sql + " ORDER BY a.start_at", *args)

    # Work orders ---------------------------------------------------------
    def create_work_order(self, customer_id: int, vehicle_id: int, complaint: str,
                          appointment_id: int | None = None) -> dict:
        self._require("customers", customer_id)
        v = self._require("vehicles", vehicle_id)
        if v["customer_id"] != customer_id:
            raise ToolError(f"Vehicle {vehicle_id} does not belong to customer {customer_id}")
        if appointment_id is not None:
            self._require("appointments", appointment_id)
        cur = self.conn.execute(
            "INSERT INTO work_orders (customer_id, vehicle_id, appointment_id, complaint) VALUES (?,?,?,?)",
            (customer_id, vehicle_id, appointment_id, complaint),
        )
        return self.get_work_order(cur.lastrowid)

    def _editable_work_order(self, work_order_id: int) -> dict:
        wo = self._require("work_orders", work_order_id)
        if self._one("SELECT id FROM invoices WHERE work_order_id = ?", work_order_id):
            raise ToolError(f"Work order {work_order_id} is already invoiced and can't be changed")
        return wo

    def add_work_order_line(self, work_order_id: int, kind: str, description: str, quantity: float,
                            unit_price: float | None = None) -> dict:
        self._editable_work_order(work_order_id)
        if quantity <= 0:
            raise ToolError("quantity must be positive")
        if unit_price is None:
            if kind != "labor":
                raise ToolError("unit_price is required for parts and fees")
            price_cents = self.cfg.labor_rate_cents
        else:
            if unit_price < 0:
                raise ToolError("unit_price can't be negative")
            price_cents = round(unit_price * 100)
        self.conn.execute(
            "INSERT INTO work_order_lines (work_order_id, kind, description, quantity, unit_price_cents) VALUES (?,?,?,?,?)",
            (work_order_id, kind, description, quantity, price_cents),
        )
        return self.get_work_order(work_order_id)

    def update_work_order(self, work_order_id: int, status: str | None = None, notes: str | None = None) -> dict:
        wo = self._require("work_orders", work_order_id)
        if status:
            self.conn.execute("UPDATE work_orders SET status=? WHERE id=?", (status, work_order_id))
        if notes:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            combined = ((wo["notes"] or "") + f"\n[{stamp}] {notes}").strip()
            self.conn.execute("UPDATE work_orders SET notes=? WHERE id=?", (combined, work_order_id))
        return self.get_work_order(work_order_id)

    def _totals(self, work_order_id: int) -> dict:
        lines = self._all("SELECT * FROM work_order_lines WHERE work_order_id = ? ORDER BY id", work_order_id)
        lines_total = taxable = 0
        for ln in lines:
            ln["line_total_cents"] = round(ln["quantity"] * ln["unit_price_cents"])
            lines_total += ln["line_total_cents"]
            if ln["kind"] != "labor" or self.cfg.tax_labor:
                taxable += ln["line_total_cents"]
        rate = self.cfg.tax_rate
        if self.cfg.tax_inclusive:
            # Prices already include tax: the total is the sum of the lines, and the
            # tax inside it is rate/(1+rate) of the taxable part (1/11 for 10% GST).
            tax = round(taxable * rate / (1 + rate))
            total = lines_total
        else:
            tax = round(taxable * rate)
            total = lines_total + tax
        # subtotal_cents is always the amount before tax, so subtotal + tax = total.
        return {"lines": lines, "subtotal_cents": total - tax, "tax_cents": tax, "total_cents": total,
                "prices_include_tax": self.cfg.tax_inclusive}

    def get_work_order(self, work_order_id: int) -> dict:
        wo = self._require("work_orders", work_order_id)
        wo["customer"] = self._one("SELECT * FROM customers WHERE id = ?", wo["customer_id"])
        wo["vehicle"] = self._one("SELECT * FROM vehicles WHERE id = ?", wo["vehicle_id"])
        wo.update(self._totals(work_order_id))
        inv = self._one("SELECT id, status FROM invoices WHERE work_order_id = ?", work_order_id)
        wo["invoice"] = inv
        return wo

    def list_work_orders(self, status: str | None = None, customer_id: int | None = None) -> list[dict]:
        sql = ("SELECT w.id, w.status, w.complaint, w.created_at, c.name AS customer_name, "
               "v.year, v.make, v.model FROM work_orders w JOIN customers c ON c.id = w.customer_id "
               "JOIN vehicles v ON v.id = w.vehicle_id WHERE 1=1")
        args: list[Any] = []
        if status:
            sql += " AND w.status = ?"
            args.append(status)
        if customer_id is not None:
            sql += " AND w.customer_id = ?"
            args.append(customer_id)
        return self._all(sql + " ORDER BY w.id DESC", *args)

    # Invoices & payments ---------------------------------------------------
    def create_invoice(self, work_order_id: int) -> dict:
        self._editable_work_order(work_order_id)
        wo = self.get_work_order(work_order_id)
        if not wo["lines"]:
            raise ToolError("Work order has no lines to invoice")
        cur = self.conn.execute(
            "INSERT INTO invoices (work_order_id, subtotal_cents, tax_cents, total_cents) VALUES (?,?,?,?)",
            (work_order_id, wo["subtotal_cents"], wo["tax_cents"], wo["total_cents"]),
        )
        invoice_id = cur.lastrowid
        self.conn.execute("UPDATE work_orders SET status='ready' WHERE id=?", (work_order_id,))
        path = self._render_invoice(invoice_id, wo)
        self.conn.execute("UPDATE invoices SET file_path=? WHERE id=?", (str(path), invoice_id))
        return self.get_invoice(invoice_id)

    def record_payment(self, invoice_id: int, amount: float, method: str) -> dict:
        inv = self._require("invoices", invoice_id)
        cents = round(amount * 100)
        if cents <= 0:
            raise ToolError("amount must be positive")
        paid = self._paid_cents(invoice_id)
        if paid + cents > inv["total_cents"]:
            raise ToolError(f"Payment exceeds balance due ({_money(inv['total_cents'] - paid)})")
        self.conn.execute(
            "INSERT INTO payments (invoice_id, amount_cents, method) VALUES (?,?,?)", (invoice_id, cents, method)
        )
        status = "paid" if paid + cents == inv["total_cents"] else "partial"
        self.conn.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
        if status == "paid":
            self.conn.execute("UPDATE work_orders SET status='closed' WHERE id=?", (inv["work_order_id"],))
        return self.get_invoice(invoice_id)

    def _paid_cents(self, invoice_id: int) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE invoice_id=?", (invoice_id,)
        ).fetchone()[0]

    def get_invoice(self, invoice_id: int) -> dict:
        inv = self._require("invoices", invoice_id)
        inv["payments"] = self._all("SELECT * FROM payments WHERE invoice_id = ? ORDER BY id", invoice_id)
        inv["balance_due_cents"] = inv["total_cents"] - self._paid_cents(invoice_id)
        inv["total"] = _money(inv["total_cents"])
        inv["balance_due"] = _money(inv["balance_due_cents"])
        return inv

    def _price_list(self) -> dict:
        try:
            return json.loads(Path(self.cfg.price_list).read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise ToolError("The shop has no price list set up; ask staff for prices") from e

    def lookup_prices(self, query: str = "") -> dict:
        data = self._price_list()
        words = [_SYNONYMS.get(w, w) for w in _words(query) if w not in _STOPWORDS]
        scored = []
        for item in data["services"]:
            name = _words(item["name"])
            rest = _words(f'{item.get("category", "")} {item.get("notes", "")}')
            # A query word matches a list word it is a prefix of ("brake" ~ "brakes").
            score = sum(2 if any(t.startswith(w) for t in name) else 1 if any(t.startswith(w) for t in rest) else 0
                        for w in words)
            if not words or score:
                scored.append((score, item))
        best = max((sc for sc, _ in scored), default=0)
        results = [{**item, "quote": _quote(item)} for sc, item in scored if sc == best]
        return {
            "as_of": data.get("as_of"),
            "tax_note": (f"Prices include {self.cfg.tax_name}." if self.cfg.tax_inclusive
                         else f"{self.cfg.tax_name} is added on top of these prices."),
            "results": results,
            "note": None if results else "Not on the price list: take details and log a follow_up for a quote.",
        }

    def shop_info(self) -> dict:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return {
            "name": self.cfg.name, "address": self.cfg.address, "phone": self.cfg.phone,
            "hours": f"{', '.join(days[d] for d in self.cfg.open_days)} "
                     f"{self.cfg.open_hour}:00-{self.cfg.close_hour}:00",
            "labor_rate": _money(self.cfg.labor_rate_cents) + "/hr",
            "tax": f"{self.cfg.tax_name} {self.cfg.tax_rate:.0%}, "
                   + ("included in prices" if self.cfg.tax_inclusive else "added on top of prices"),
            "tax_applies_to_labor": self.cfg.tax_labor,
            "bays": self.cfg.bays,
        }

    def _render_invoice(self, invoice_id: int, wo: dict) -> Path:
        e = html.escape
        c, v = wo["customer"], wo["vehicle"]
        rows = "".join(
            f"<tr><td>{e(ln['kind'].title())}</td><td>{e(ln['description'])}</td>"
            f"<td class=n>{ln['quantity']:g}</td><td class=n>{_money(ln['unit_price_cents'])}</td>"
            f"<td class=n>{_money(ln['line_total_cents'])}</td></tr>"
            for ln in wo["lines"]
        )
        vehicle = " ".join(str(x) for x in (v["year"], v["make"], v["model"]) if x)
        tax = self.cfg.tax_name
        title = "Tax Invoice" if self.cfg.tax_inclusive and tax == "GST" else "Invoice"
        if self.cfg.tax_inclusive:
            totals = (
                f"<tr class=tot><td colspan=4 class=n><b>Total (incl. {e(tax)})</b></td>"
                f"<td class=n><b>{_money(wo['total_cents'])}</b></td></tr>"
                f"<tr class=tot><td colspan=4 class=n>Includes {e(tax)} of</td>"
                f"<td class=n>{_money(wo['tax_cents'])}</td></tr>"
            )
        else:
            totals = (
                f"<tr class=tot><td colspan=4 class=n>Subtotal</td><td class=n>{_money(wo['subtotal_cents'])}</td></tr>"
                f"<tr class=tot><td colspan=4 class=n>{e(tax)} ({self.cfg.tax_rate:.2%})</td>"
                f"<td class=n>{_money(wo['tax_cents'])}</td></tr>"
                f"<tr class=tot><td colspan=4 class=n><b>Total</b></td><td class=n><b>{_money(wo['total_cents'])}</b></td></tr>"
            )
        doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title} {invoice_id}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:32px auto;color:#222}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:6px;border-bottom:1px solid #ddd;text-align:left}}
.n{{text-align:right}}.tot td{{border:none}}h1{{margin-bottom:0}}</style></head><body>
<h1>{e(self.cfg.name)}</h1><p>{e(self.cfg.address)} {e(self.cfg.phone)}
{f"<br>ABN {e(self.cfg.abn)}" if self.cfg.abn else ""}</p>
<h2>{title} #{invoice_id}</h2>
<p><b>Date:</b> {datetime.now():%d %b %Y} &nbsp; <b>Work order:</b> #{wo['id']}</p>
<p><b>Customer:</b> {e(c['name'])} {e(c['phone'] or '')}<br>
<b>Vehicle:</b> {e(vehicle)} {e(v['plate'] or '')} {('VIN ' + e(v['vin'])) if v['vin'] else ''}
{f"<br><b>Odometer:</b> {v['mileage']:,}" if v['mileage'] else ''}</p>
<p><b>Customer concern:</b> {e(wo['complaint'])}</p>
<table><tr><th>Type</th><th>Description</th><th class=n>Qty</th><th class=n>Price</th><th class=n>Total</th></tr>{rows}
{totals}
</table><p>Thank you for your business!</p></body></html>"""
        self.cfg.invoice_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.invoice_dir / f"invoice-{invoice_id:05d}.html"
        path.write_text(doc, encoding="utf-8")
        return path
