import json

import pytest

from bluenile.agent_config import agent_tools
from bluenile.db import connect
from bluenile.tools import ShopConfig, ShopTools, ToolError

MONDAY = "2026-11-30"


@pytest.fixture
def shop(tmp_path):
    cfg = ShopConfig(labor_rate_cents=12000, tax_rate=0.10, tax_labor=False, bays=1,
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
