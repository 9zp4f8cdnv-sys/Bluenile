from datetime import datetime
from pathlib import Path

import pytest

from bluenile import staff, web
from bluenile.db import connect
from bluenile.tools import ShopConfig, ShopTools
from bluenile.website import build

NOW = datetime(2026, 11, 24, 9, 30)  # Tuesday
PW = "correct horse"


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "shop.db"
    cfg = ShopConfig(invoice_dir=tmp_path, bays=1)
    build(cfg, tmp_path / "site")
    app = web.create_web_app(lambda: ShopTools(connect(db), cfg), tmp_path / "site", now=lambda: NOW)
    staff.register_staff_app(app, lambda: connect(db), lambda: cfg, db, password=PW, secure_cookies=False)
    app.secret_key = "test"
    return app, db, cfg


def signed_in(app):
    c = app.test_client()
    r = c.post("/staff/login", data={"password": PW})
    assert r.status_code == 302 and r.headers["Location"].endswith("/staff/")
    return c


def put(c, coll, id, data):
    return c.put(f"/staff/api/doc/{coll}/{id}", json=data, headers={"X-Staff": "1"})


def test_staff_app_needs_sign_in(env):
    app, *_ = env
    c = app.test_client()
    assert c.get("/staff/").status_code == 302
    assert c.get("/staff/index.html").status_code == 302
    assert c.get("/staff/api/changes").status_code == 401
    assert c.put("/staff/api/doc/jobs/x", json={}, headers={"X-Staff": "1"}).status_code == 401
    assert c.get("/staff/manifest.webmanifest").status_code == 200  # needed for Add to Home Screen
    assert c.get("/staff/storage.js").status_code == 200
    assert c.get("/staff/../shop.db").status_code == 404
    r = c.post("/staff/login", data={"password": "nope"})
    assert r.status_code == 200 and b"isn't right" in r.data


def test_login_rate_limited(env):
    app, *_ = env
    c = app.test_client()
    for _ in range(10):
        c.post("/staff/login", data={"password": "nope"})
    assert b"Too many tries" in c.post("/staff/login", data={"password": PW}).data


def test_switched_off_without_password(tmp_path):
    from flask import Flask
    app = Flask(__name__)
    staff.register_staff_app(app, lambda: connect(tmp_path / "db"), ShopConfig, tmp_path / "db", password="")
    assert app.test_client().get("/staff/login").status_code == 503
    assert app.test_client().get("/staff/").status_code == 302


def test_writes_need_staff_header(env):
    c = signed_in(env[0])
    assert c.put("/staff/api/doc/jobs/j1", json={"a": 1}).status_code == 403  # e.g. a cross-site form
    assert put(c, "nope", "j1", {}).status_code == 400
    assert put(c, "jobs", "bad id", {}).status_code == 400
    assert put(c, "jobs", "j1", ["not", "an", "object"]).status_code == 400


def test_two_phones_share_data(env):
    app, *_ = env
    a, b = signed_in(app), signed_in(app)
    start = b.get("/staff/api/changes?since=0").get_json()
    # business details are seeded from shop_details.json
    biz = [d for d in start["docs"] if d["c"] == "settings"][0]["data"]
    assert biz["abn"] == "11621473514" and biz["phone"] == "0434 609 834"
    rev = start["rev"]
    assert put(a, "jobs", "j1", {"rego": "ABC123", "status": "open"}).status_code == 200
    ch = b.get(f"/staff/api/changes?since={rev}").get_json()
    assert ch["docs"] == [{"c": "jobs", "id": "j1", "data": {"rego": "ABC123", "status": "open"}}]
    assert a.delete("/staff/api/doc/jobs/j1", headers={"X-Staff": "1"}).status_code == 200
    ch2 = b.get(f"/staff/api/changes?since={ch['rev']}").get_json()
    assert ch2["docs"] == [{"c": "jobs", "id": "j1", "data": None}]


def booking(**over):
    b = {"items": [{"name": "Wheel alignment", "qty": 1}], "vehicle": {"rego": "XYZ 789", "make": "Mazda", "model": "3"},
         "customer": {"name": "Pat Lee", "phone": "0400 000 111"}, "slot": "2026-11-25T09:00"}
    b.update(over)
    return b


def test_online_booking_shows_in_staff_app_and_cancel_frees_slot(env):
    app, db, cfg = env
    pub = app.test_client()
    j = pub.post("/api/bookings", json=booking()).get_json()
    c = signed_in(app)
    bk = {d["id"]: d["data"] for d in c.get("/staff/api/changes?since=0").get_json()["docs"] if d["c"] == "bookings"}
    doc = bk[f"web-{j['booking_id']}"]
    assert doc["name"] == "Pat Lee" and doc["rego"] == "XYZ789" and doc["date"] == "2026-11-25"
    assert doc["time"] == "09:00" and doc["status"] == "booked" and doc["source"] == "online"
    assert "Wheel alignment" in doc["service"]
    # one bay: 9:00 is taken online
    assert "2026-11-25T09:00" not in pub.get("/api/availability?date=2026-11-25").get_json()["free_slots"]
    put(c, "bookings", f"web-{j['booking_id']}", {**doc, "status": "cancelled"})
    assert "2026-11-25T09:00" in pub.get("/api/availability?date=2026-11-25").get_json()["free_slots"]


def test_staff_app_bookings_block_online_slots(env):
    app, *_ = env
    c = signed_in(app)
    put(c, "bookings", "b1", {"name": "Walk-in", "date": "2026-11-26", "time": "10:00", "status": "booked"})
    free = app.test_client().get("/api/availability?date=2026-11-26").get_json()["free_slots"]
    # 2 h bookings: 9:00 and 10:00 and 11:00 starts overlap 10:00-12:00
    assert "2026-11-26T08:00" in free and "2026-11-26T12:00" in free
    assert not {"2026-11-26T09:00", "2026-11-26T10:00", "2026-11-26T11:00"} & set(free)
    r = app.test_client().post("/api/bookings", json=booking(slot="2026-11-26T10:00"))
    assert r.status_code == 409


def test_stock_on_website_with_gst_and_bookable(env):
    app, db, cfg = env
    c = signed_in(app)
    put(c, "parts", "T-2254517-MICHE", {"name": "Michelin Primacy 4 225/45R17", "category": "tyre", "brand": "Michelin",
                                         "model": "Primacy 4", "size": "225/45R17", "price": 210, "qty": 3, "reorder": 2})
    put(c, "parts", "T-OUT", {"name": "Out", "category": "tyre", "size": "205/55R16", "price": 100, "qty": 0})
    put(c, "parts", "BRK-PAD", {"name": "Pads", "category": "brakes", "price": 89, "qty": 5})
    items = app.test_client().get("/api/stock").get_json()["items"]
    assert [i["sku"] for i in items] == ["T-2254517-MICHE"]  # in-stock tyres/wheels only
    assert items[0]["price_cents"] == 23100 and items[0]["max_qty"] == 3  # $210 ex GST -> $231 incl.
    assert "available" not in items[0] and "cost" not in items[0]

    pub = app.test_client()
    r = pub.post("/api/bookings", json=booking(items=[], stock=[{"sku": "T-2254517-MICHE", "qty": 4}]))
    assert r.status_code == 409 and "only have 3" in r.get_json()["error"]
    j = pub.post("/api/bookings", json=booking(items=[], stock=[{"sku": "T-2254517-MICHE", "qty": 2}])).get_json()
    assert j["total_cents"] == 46200
    assert j["priced_items"] == [{"name": "Tyre: Michelin Primacy 4 225/45R17", "qty": 2, "unit_cents": 23100}]
    wo = ShopTools(connect(db), cfg).get_work_order(j["work_order_id"])
    assert wo["lines"][0]["kind"] == "part" and wo["lines"][0]["unit_price_cents"] == 23100
    r = pub.post("/api/bookings", json=booking(items=[], stock=[{"sku": "GONE", "qty": 1}], slot="2026-11-26T09:00"))
    assert r.status_code == 409 and "no longer in stock" in r.get_json()["error"]


def test_tyre_size_key():
    assert staff.tyre_size_key("225/45R17") == staff.tyre_size_key("225/45 R17") == "2254517"
