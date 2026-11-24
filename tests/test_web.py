from datetime import datetime
from pathlib import Path

import pytest

from bluenile import web
from bluenile.db import connect
from bluenile.tools import ShopConfig, ShopTools
from bluenile.website import build

NOW = datetime(2026, 11, 24, 9, 30)  # a Tuesday


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "shop.db"
    cfg = ShopConfig(invoice_dir=tmp_path, bays=1)
    build(cfg, tmp_path / "site")
    app = web.create_web_app(lambda: ShopTools(connect(db), cfg), tmp_path / "site", now=lambda: NOW)
    c = app.test_client()
    c.db = db
    c.cfg = cfg
    return c


def booking(**over):
    b = {
        "items": [{"name": "Standard service - normal cars", "qty": 1}, {"name": "Cheap wipers", "qty": 2},
                  {"name": "Brake service", "qty": 1}, {"name": "Cabin filter", "qty": 1}],
        "tyres": {"width": 205, "profile": 55, "rim": 16, "qty": 4},
        "vehicle": {"rego": "abc 123", "make": "Mazda", "model": "3", "year": "2018"},
        "customer": {"name": "Sam Taylor", "phone": "0412 345 678", "email": "sam@example.com"},
        "slot": "2026-11-25T09:00", "notes": "Squeaky brakes",
    }
    b.update(over)
    return b


def test_site_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"shop-data" in r.data


def test_availability_hides_past_and_short_notice_slots(client):
    today = client.get("/api/availability?date=2026-11-24&hours=1").get_json()
    assert today["free_slots"][0] == "2026-11-24T11:00"  # 9:30 now + 1 h notice
    assert client.get("/api/availability?date=2026-11-23").get_json()["free_slots"] == []
    assert client.get("/api/availability?date=nope").status_code == 400


def test_booking_lands_in_workshop_system(client):
    r = client.post("/api/bookings", json=booking())
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    # $249 service + 2 x $25 wipers are fixed; brake service, cabin filter range and tyres need quotes
    assert j["total_cents"] == 24900 + 5000
    assert set(j["quote_items"]) == {"Brake service", "Cabin filter", "4 x tyres 205/55R16"}
    assert j["start"] == "2026-11-25T09:00" and j["end"] == "2026-11-25T11:00"  # every booking is 2 h

    t = ShopTools(connect(client.db), client.cfg)
    wo = t.get_work_order(j["work_order_id"])
    assert wo["complaint"] == "Squeaky brakes"
    assert [(l["description"], l["quantity"], l["unit_price_cents"]) for l in wo["lines"]] == [
        ("Standard service - normal cars", 1.0, 24900), ("Cheap wipers", 2.0, 2500)]
    assert "Quote needed" in wo["notes"] and "205/55R16" in wo["notes"]
    cust = t.find_customer("0412345678")[0]
    assert cust["vehicles"][0]["plate"] == "ABC123"

    # same customer books again: matched by phone, vehicle matched by rego
    r2 = client.post("/api/bookings", json=booking(slot="2026-11-26T09:00", tyres=None,
                                                    customer={"name": "S Taylor", "phone": "0412345678"}))
    assert r2.status_code == 201
    assert len(t.find_customer("Taylor")) == 1 and len(t.find_customer("Taylor")[0]["vehicles"]) == 1


def test_slot_taken_returns_409(client):
    assert client.post("/api/bookings", json=booking()).status_code == 201
    r = client.post("/api/bookings", json=booking(customer={"name": "Other", "phone": "0499 999 999"}))
    assert r.status_code == 409


def test_client_cannot_set_prices(client):
    b = booking(items=[{"name": "Wheel alignment", "qty": 1, "price": 1}], tyres=None)
    assert client.post("/api/bookings", json=b).get_json()["total_cents"] == 9000


@pytest.mark.parametrize("over,msg", [
    ({"items": [{"name": "Free car", "qty": 1}], "tyres": None}, "Unknown service"),
    ({"items": [], "tyres": None}, "at least one"),
    ({"items": [{"name": "Wheel alignment", "qty": 9}]}, "Quantity"),
    ({"tyres": {"width": 5, "profile": 55, "rim": 16, "qty": 4}}, "Tyre size"),
    ({"customer": {"name": "A", "phone": "123"}}, "phone"),
    ({"customer": {"name": "", "phone": "0412345678"}}, "name"),
    ({"vehicle": {"make": "", "model": "x"}}, "make"),
    ({"slot": "2026-11-24T09:00"}, "passed"),
    ({"slot": "2027-06-01T09:00"}, "60 days"),
    ({"company": "spam inc"}, "Invalid"),
])
def test_bad_bookings_rejected(client, over, msg):
    r = client.post("/api/bookings", json=booking(**over))
    assert r.status_code == 400 and msg in r.get_json()["error"]


def test_closed_day_rejected(client):
    r = client.post("/api/bookings", json=booking(slot="2026-11-29T09:00"))  # Sunday
    assert r.status_code == 409 and "closed" in r.get_json()["error"]


def test_rate_limit(client, monkeypatch):
    for i in range(web.RATE_LIMIT[0]):
        client.post("/api/bookings", json={"items": []})
    assert client.post("/api/bookings", json=booking()).status_code == 429


def test_every_booking_is_two_hours(client):
    j = client.post("/api/bookings", json=booking(items=[{"name": "Light (globe)", "qty": 1}], tyres=None)).get_json()
    assert j["end"] == "2026-11-25T11:00"
    # and availability only offers starts with 2 h left before closing (6pm)
    slots = client.get("/api/availability?date=2026-11-26&hours=1").get_json()["free_slots"]
    assert slots[-1] == "2026-11-26T16:00"


@pytest.mark.parametrize("sent,make,model,expected,price", [
    ("Standard service - very big cars", "Mazda", "3", "Standard service - normal cars", 24900),
    ("Standard service - normal cars", "Toyota", "LandCruiser 200", "Standard service - very big cars", 32900),
    ("Standard service - normal cars", "VW", "Golf", "Standard service - large cars and EU", 29900),
])
def test_service_price_comes_from_the_vehicle(client, sent, make, model, expected, price):
    b = booking(items=[{"name": sent, "qty": 1}], tyres=None, vehicle={"make": make, "model": model})
    j = client.post("/api/bookings", json=b).get_json()
    assert j["priced_items"] == [{"name": expected, "qty": 1, "unit_cents": price}]
    t = ShopTools(connect(client.db), client.cfg)
    assert "Service price from make/model" in t.get_work_order(j["work_order_id"])["notes"]


def test_classify_endpoint(client):
    j = client.get("/api/classify?make=Ford&model=Ranger XLT").get_json()
    assert j["tier"] == "Standard service - very big cars" and j["quote"] == "$329"
    assert client.get("/api/classify?make=").status_code == 400


def test_rego_lookup_needs_a_provider(client, tmp_path):
    assert client.get("/api/vehicle?rego=ABC123").status_code == 404
    found = {"ABC123": {"make": "Toyota", "model": "HiLux", "year": 2020}}
    app = web.create_web_app(lambda: ShopTools(connect(client.db), client.cfg), tmp_path / "site",
                             now=lambda: NOW, rego_lookup=lambda rego, state: dict(found[rego]) if rego in found else None)
    c = app.test_client()
    j = c.get("/api/vehicle?rego=abc 123&state=VIC").get_json()
    assert j["model"] == "HiLux" and j["tier"] == "Standard service - very big cars"
    assert c.get("/api/vehicle?rego=ZZZ999").status_code == 404
