"""SQLite storage for the shop: customers, vehicles, calls, appointments,
work orders, invoices and payments.

Money is stored in integer cents to avoid floating-point rounding errors.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("BLUENILE_DB", "shop.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    year        INTEGER,
    make        TEXT NOT NULL,
    model       TEXT NOT NULL,
    vin         TEXT,
    plate       TEXT,
    mileage     INTEGER
);

CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    caller_name TEXT,
    phone       TEXT,
    intent      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    follow_up   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS appointments (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    vehicle_id  INTEGER REFERENCES vehicles(id),
    start_at    TEXT NOT NULL,          -- ISO 'YYYY-MM-DDTHH:MM'
    end_at      TEXT NOT NULL,
    service     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled'  -- scheduled | cancelled | completed
);

CREATE TABLE IF NOT EXISTS work_orders (
    id             INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES customers(id),
    vehicle_id     INTEGER NOT NULL REFERENCES vehicles(id),
    appointment_id INTEGER REFERENCES appointments(id),
    complaint      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS work_order_lines (
    id               INTEGER PRIMARY KEY,
    work_order_id    INTEGER NOT NULL REFERENCES work_orders(id),
    kind             TEXT NOT NULL,     -- labor | part | fee
    description      TEXT NOT NULL,
    quantity         REAL NOT NULL,
    unit_price_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY,
    work_order_id  INTEGER NOT NULL UNIQUE REFERENCES work_orders(id),
    subtotal_cents INTEGER NOT NULL,
    tax_cents      INTEGER NOT NULL,
    total_cents    INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'unpaid',  -- unpaid | partial | paid
    file_path      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL,
    method       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
