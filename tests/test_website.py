import json

from bluenile.tools import ShopConfig
from bluenile.website import build, opening_hours, page_data, render, web_price


def prices():
    return json.loads(ShopConfig().price_list.read_text())


def services(page):
    return {s["name"]: s for s in page_data(page)["services"]}


def test_web_price_rules():
    assert web_price({"confirmed": True, "price_min": 249, "price_max": 249}) == "$249"
    assert web_price({"confirmed": True, "price_min": 100, "price_max": 150}) == "$100–$150"
    assert web_price({"confirmed": True, "price_min": 200, "price_max": None}) == "From $200"
    assert web_price({"confirmed": True, "price_min": 130, "price_max": 130, "unit": "per hour"}) == "$130 per hour"
    assert web_price({"confirmed": False, "price_min": 73, "price_max": 73}) is None
    assert web_price({"confirmed": True, "price_min": None, "price_max": None}) is None


def test_public_data_has_confirmed_prices_only():
    s = services(render(ShopConfig(), prices()))
    assert s["Standard service - normal cars"]["fixed_cents"] == 24900
    assert s["Standard service - large cars and EU"]["display"] == "Standard service"
    assert s["Standard service - large cars and EU"]["tier"] is True
    assert s["RWC (roadworthy certificate)"]["fixed_cents"] == 9700
    assert s["Diagnostic / labour"]["fixed_cents"] == 13000
    # ranges show a label and a "from" amount, never a fixed price
    assert s["Cabin filter"]["fixed_cents"] is None and s["Cabin filter"]["price_label"] == "$49–$69"
    assert s["Cabin filter"]["from_cents"] == 4900
    # unconfirmed: no price at all on the page
    for name in ["Rotate and balance tyres", "Brake pads machining", "Engine flush", "Brake service", "Headlight polish"]:
        assert s[name]["fixed_cents"] is None and s[name]["price_label"] is None and s[name]["from_cents"] is None, name


def test_internal_notes_never_published():
    page = render(ShopConfig(), prices())
    for internal in ["not legible", "unclear", "not yet confirmed", "Shown as", "confirmed\""]:
        assert internal not in page, internal
    assert "Flush only. We don't offer a transmission service for VW." in json.dumps(page_data(page), ensure_ascii=False)


def test_missing_contact_details_reported(tmp_path):
    path, missing = build(ShopConfig(phone="", address="", email="", abn=""), tmp_path)
    assert page_data(path.read_text())["shop"]["phone"] == "" and len(missing) == 4


def test_contact_details_and_escaping(tmp_path):
    cfg = ShopConfig(name="Joe's <Garage>", phone="(03) 9000 0000", address="1 Main St", email="a@b.com", abn="12 345")
    path, missing = build(cfg, tmp_path, api_base="https://book.example.com/")
    page = path.read_text()
    assert missing == []
    data = page_data(page)
    assert data["shop"]["tel"] == "0390000000" and data["shop"]["abn"] == "12 345"
    assert data["api"] == "https://book.example.com"
    assert "<Garage>" not in page and "&lt;Garage&gt;" in page


def test_script_breakout_is_impossible(tmp_path):
    cfg = ShopConfig(name="</script><script>alert(1)</script>")
    page = render(cfg, prices())
    assert page.count("</script>") == 2  # the data block and the app script only
    assert page_data(page)["shop"]["name"] == cfg.name


def test_opening_hours_grouping():
    assert opening_hours(ShopConfig()) == [("Monday – Saturday", "8:00am – 6:00pm"), ("Sunday", "Closed")]
    weekdays = ShopConfig(open_days=(0, 1, 2, 3, 4), open_hour=7, close_hour=18)
    assert opening_hours(weekdays) == [("Monday – Friday", "7:00am – 6:00pm"), ("Saturday – Sunday", "Closed")]


def test_page_carries_vehicle_rules_and_no_size_labels():
    page = render(ShopConfig(), prices())
    data = page_data(page)
    assert data["vehicle_rules"]["default"] == "Standard service - normal cars"
    assert data["shop"]["booking_hours"] == 2
    assert "Toyota" in data["makes"]
    for label in ["Large &amp; European cars", "Large & European cars", "Pick your car size"]:
        assert label not in page
