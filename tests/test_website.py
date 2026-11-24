import json

from bluenile.tools import ShopConfig
from bluenile.website import build, opening_hours, render, web_price


def prices():
    return json.loads(ShopConfig().price_list.read_text())


def test_web_price_rules():
    assert web_price({"confirmed": True, "price_min": 249, "price_max": 249}) == "$249"
    assert web_price({"confirmed": True, "price_min": 100, "price_max": 150}) == "$100–$150"
    assert web_price({"confirmed": True, "price_min": 200, "price_max": None}) == "From $200"
    assert web_price({"confirmed": True, "price_min": 130, "price_max": 130, "unit": "per hour"}) == "$130 per hour"
    assert web_price({"confirmed": False, "price_min": 73, "price_max": 73}) is None
    assert web_price({"confirmed": True, "price_min": None, "price_max": None}) is None


def test_page_shows_confirmed_prices_and_hides_internal_notes():
    page = render(ShopConfig(), prices())
    for text in ["$249", "$299", "$329", "RWC (roadworthy certificate)", "$97", "$130 per hour",
                 "All prices include GST.", "Flush only. We don&#x27;t offer a transmission service for VW."]:
        assert text in page, text
    # unconfirmed entries are not priced publicly
    assert "$73" not in page and "$425" not in page and "$350" not in page
    assert page.count("Call for a quote") >= 9
    for internal in ["not legible", "unclear", "not yet confirmed", "Shown as"]:
        assert internal not in page, internal


def test_missing_contact_details_are_left_out(tmp_path):
    path, missing = build(ShopConfig(phone="", address="", email="", abn=""), tmp_path)
    page = path.read_text()
    assert "tel:" not in page and "Contact details coming soon." in page
    assert len(missing) == 4


def test_contact_details_and_escaping(tmp_path):
    cfg = ShopConfig(name="Joe's <Garage>", phone="(03) 9000 0000", address="1 Main St", email="a@b.com", abn="12 345")
    path, missing = build(cfg, tmp_path)
    page = path.read_text()
    assert missing == []
    assert 'href="tel:0390000000"' in page and "mailto:a@b.com" in page and "ABN 12 345" in page
    assert "<Garage>" not in page and "&lt;Garage&gt;" in page


def test_opening_hours_grouping():
    assert opening_hours(ShopConfig()) == [("Monday – Saturday", "8:00am – 5:00pm"), ("Sunday", "Closed")]
    weekdays = ShopConfig(open_days=(0, 1, 2, 3, 4), open_hour=7, close_hour=18)
    assert opening_hours(weekdays) == [("Monday – Friday", "7:00am – 6:00pm"), ("Saturday – Sunday", "Closed")]
