import json

import pytest

from bluenile.agent_config import agent_tools
from bluenile.db import connect
from bluenile.tools import ShopConfig, ShopTools, ToolError

MONDAY = "2026-11-30"


@pytest.fixture
def shop(tmp_path):
    cfg = ShopConfig(labor_rate_cents=12000, tax_rate=0.10, tax_inclusive=False, tax_labor=False, bays=1,
                     open_hour=8, close_hour=12, invoice_dir=tmp_path / "invoices")
    return ShopTools(connect(tmp_path / "shop.db"), cfg)


def call(shop, tool, **kw):
    return json.loads(shop.run(tool, kw))


def make_customer(shop):
    c = call(shop, "save_customer", name="Dana Ruiz", phone="(555) 201-3344")
    v = call(shop, "add_vehicle", customer_id=c["id"], year=2017, make="Honda", model="Civic", plate="7ABC123")
    return c, v


def test_definitions_are_valid_custom_tools(shop):
    defs = agent_tools(shop)
    assert defs[0]["type"] == "agent_toolset_20260401"
    names = [d["name"] for d in defs[1:]]
    assert len(names) == len(set(names))
    for d in defs[1:]:
        assert d["type"] == "custom" and d["input_schema"]["type"] == "object"
        assert set(d["input_schema"].get("required", [])) <= set(d["input_schema"]["properties"])
    assert {"end_call", "transfer_to_staff"} <= set(names)


def test_find_customer_by_phone_digits_and_plate(shop):
    c, _ = make_customer(shop)
    assert call(shop, "find_customer", query="555-201-3344")[0]["id"] == c["id"]
    found = call(shop, "find_customer", query="7abc")
    assert found[0]["vehicles"][0]["make"] == "Honda"
    assert call(shop, "find_customer", query="nobody") == []


def test_scheduling_respects_bays_and_hours(shop):
    c, v = make_customer(shop)
    slots = call(shop, "check_availability", date=MONDAY, duration_hours=2)["free_slots"]
    assert slots == [f"{MONDAY}T08:00", f"{MONDAY}T09:00", f"{MONDAY}T10:00"]
    call(shop, "book_appointment", customer_id=c["id"], vehicle_id=v["id"],
         start=f"{MONDAY}T09:00", duration_hours=1, service="Oil change")
    slots = call(shop, "check_availability", date=MONDAY)["free_slots"]
    assert f"{MONDAY}T09:00" not in slots and f"{MONDAY}T10:00" in slots
    with pytest.raises(ToolError, match="No free bay"):
        shop.run("book_appointment", {"customer_id": c["id"], "start": f"{MONDAY}T09:00", "service": "x"})
    with pytest.raises(ToolError, match="Outside shop hours"):
        shop.run("book_appointment", {"customer_id": c["id"], "start": f"{MONDAY}T11:30", "service": "x"})
    assert call(shop, "check_availability", date="2026-12-06")["open"] is False  # Sunday


def test_work_order_invoice_and_payment(shop, tmp_path):
    c, v = make_customer(shop)
    wo = call(shop, "create_work_order", customer_id=c["id"], vehicle_id=v["id"], complaint="Grinding brakes")
    call(shop, "add_work_order_line", work_order_id=wo["id"], kind="labor", description="Front pads", quantity=1.5)
    call(shop, "add_work_order_line", work_order_id=wo["id"], kind="part", description="Pad set", quantity=1, unit_price=64.99)
    inv = call(shop, "create_invoice", work_order_id=wo["id"])
    # labor 1.5 * 120 = 180.00 untaxed; part 64.99 taxed at 10% = 6.50
    assert inv["subtotal_cents"] == 24499 and inv["tax_cents"] == 650 and inv["total_cents"] == 25149
    page = (tmp_path / "invoices" / "invoice-00001.html").read_text()
    assert "Dana Ruiz" in page and "$251.49" in page

    with pytest.raises(ToolError, match="already invoiced"):
        shop.run("add_work_order_line", {"work_order_id": wo["id"], "kind": "fee", "description": "x", "quantity": 1, "unit_price": 1})
    with pytest.raises(ToolError, match="exceeds balance"):
        shop.run("record_payment", {"invoice_id": inv["id"], "amount": 300, "method": "card"})

    assert call(shop, "record_payment", invoice_id=inv["id"], amount=100, method="cash")["status"] == "partial"
    paid = call(shop, "record_payment", invoice_id=inv["id"], amount=151.49, method="card")
    assert paid["status"] == "paid" and paid["balance_due_cents"] == 0
    assert call(shop, "get_work_order", work_order_id=wo["id"])["status"] == "closed"


def test_html_is_escaped(shop, tmp_path):
    c = call(shop, "save_customer", name="<script>x</script>")
    v = call(shop, "add_vehicle", customer_id=c["id"], make="Ford", model="F-150")
    wo = call(shop, "create_work_order", customer_id=c["id"], vehicle_id=v["id"], complaint="noise")
    call(shop, "add_work_order_line", work_order_id=wo["id"], kind="fee", description="Shop fee", quantity=1, unit_price=5)
    call(shop, "create_invoice", work_order_id=wo["id"])
    assert "<script>" not in (tmp_path / "invoices" / "invoice-00001.html").read_text()


def test_calls_and_bad_input(shop):
    call(shop, "log_call", intent="quote", summary="Wants brake quote", phone="555-0000", follow_up="Call back with price")
    call(shop, "log_call", intent="status_check", summary="Asked if car ready")
    assert len(call(shop, "list_calls", only_follow_ups=True)) == 1
    with pytest.raises(ToolError, match="No customer"):
        shop.run("add_vehicle", {"customer_id": 99, "make": "x", "model": "y"})
    with pytest.raises(ToolError, match="Unknown tool"):
        shop.run("nope", {})
    with pytest.raises(ToolError, match="Bad arguments"):
        shop.run("find_customer", {"wrong": 1})


def test_lookup_prices(shop):
    full = call(shop, "lookup_prices")
    assert full["as_of"] == "2026-11-20" and len(full["results"]) > 20

    svc = call(shop, "lookup_prices", query="service")["results"]
    assert "Standard service - normal cars" in [r["name"] for r in svc]

    tyres = call(shop, "lookup_prices", query="rotate and balance tires")["results"]
    assert tyres[0]["name"] == "Rotate and balance tyres" and tyres[0]["confirmed"] is False

    brake = [r for r in call(shop, "lookup_prices", query="brake service")["results"] if r["name"] == "Brake service"]
    assert brake and brake[0]["quote"] == "no price on the list"

    assert call(shop, "lookup_prices", query="aircon regas")["results"][0]["quote"] == "$150"
    assert call(shop, "lookup_prices", query="pre purchase inspection")["results"][0]["quote"] == "$100 to $150"
    assert call(shop, "lookup_prices", query="transmission flush")["results"][0]["quote"] == "from $200"
    assert call(shop, "lookup_prices", query="cheap wipers")["results"][0]["quote"] == "$25 each ($50 a pair)"

    missing = call(shop, "lookup_prices", query="turbo rebuild")
    assert missing["results"] == [] and "follow_up" in missing["note"]


def test_price_list_file_is_consistent():
    from bluenile.tools import ShopConfig as C
    data = json.loads(C().price_list.read_text())
    names = [s["name"] for s in data["services"]]
    assert len(names) == len(set(names))
    for s in data["services"]:
        assert isinstance(s["confirmed"], bool)
        lo, hi = s["price_min"], s["price_max"]
        if lo is not None and hi is not None:
            assert lo <= hi


def test_defaults_are_gst_inclusive_at_130_an_hour():
    cfg = ShopConfig()
    assert cfg.labor_rate_cents == 13000 and cfg.tax_rate == 0.10
    assert cfg.tax_inclusive and cfg.tax_labor and cfg.tax_name == "GST"


def test_gst_inclusive_invoice(tmp_path):
    cfg = ShopConfig(invoice_dir=tmp_path, abn="12 345 678 901", bays=1)
    shop = ShopTools(connect(tmp_path / "shop.db"), cfg)
    c, v = make_customer(shop)
    wo = call(shop, "create_work_order", customer_id=c["id"], vehicle_id=v["id"], complaint="Service due")
    call(shop, "add_work_order_line", work_order_id=wo["id"], kind="labor", description="Diagnostic", quantity=1.5)
    call(shop, "add_work_order_line", work_order_id=wo["id"], kind="fee", description="Standard service", quantity=1, unit_price=249)
    inv = call(shop, "create_invoice", work_order_id=wo["id"])
    # 1.5 h x $130 = $195 + $249 = $444 total; GST inside it = 444 / 11 = $40.36
    assert inv["total_cents"] == 44400 and inv["tax_cents"] == 4036 and inv["subtotal_cents"] == 40364
    page = (tmp_path / "invoice-00001.html").read_text()
    assert "<h1>Tax Invoice</h1>" in page and "ABN 12 345 678 901" in page and "Blue Nile emblem" in page
    assert "Total (incl. GST)" in page and "$444.00" in page and "Includes GST of" in page and "$40.36" in page
    assert call(shop, "lookup_prices", query="rwc")["tax_note"] == "Prices include GST."
    paid = call(shop, "record_payment", invoice_id=inv["id"], amount=444, method="card")
    assert paid["status"] == "paid"


def test_settings_come_from_details_file_and_env_overrides(monkeypatch):
    from bluenile.tools import setting
    monkeypatch.delenv("SHOP_PHONE", raising=False)
    assert setting("SHOP_PHONE") == "0434 609 834"
    assert setting("SHOP_ABN") == "11 621 473 514"
    assert setting("SHOP_EMAIL") == "bluenileautotyres@gmail.com"
    assert setting("SHOP_NOT_IN_FILE", "none") == "none"
    monkeypatch.setenv("SHOP_PHONE", "(07) 0000 0000")
    assert setting("SHOP_PHONE") == "(07) 0000 0000"
