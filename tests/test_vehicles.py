import json

import pytest

from bluenile.db import connect
from bluenile.tools import ShopConfig, ShopTools
from bluenile.vehicles import classify, load_rules

NORMAL, LARGE_EU, BIG = ("Standard service - normal cars", "Standard service - large cars and EU",
                         "Standard service - very big cars")


@pytest.mark.parametrize("make,model,tier", [
    ("Toyota", "Corolla", NORMAL),
    ("Toyota", "LandCruiser 200 GXL", BIG),
    ("toyota", "Land Cruiser", BIG),
    ("Ford", "Ranger XLT", BIG),
    ("Ford", "Falcon", LARGE_EU),
    ("VW", "Golf", LARGE_EU),
    ("Volkswagen", "Amarok", BIG),        # model rule beats the European make rule
    ("Mercedes Benz", "Sprinter 316", BIG),
    ("merc", "A180", LARGE_EU),
    ("Range Rover", "Sport", BIG),
    ("Mazda", "CX-5", NORMAL),
    ("Kia", "Carnival", BIG),
    ("Hyundai", "i30", NORMAL),
    ("Unknown", "Thing", NORMAL),
])
def test_classify(make, model, tier):
    assert classify(make, model)["tier"] == tier


def test_rules_point_at_real_price_list_rows():
    names = {s["name"] for s in json.loads(ShopConfig().price_list.read_text())["services"]}
    rules = load_rules()
    assert rules["default"] in names
    assert all(r["tier"] in names for r in rules["rules"])


def test_agent_tool(tmp_path):
    t = ShopTools(connect(tmp_path / "db"), ShopConfig(invoice_dir=tmp_path))
    r = json.loads(t.run("classify_vehicle", {"make": "Nissan", "model": "Patrol"}))
    assert r["tier"] == BIG and r["quote"] == "$329" and r["confirmed"] is True


def test_find_spec_matches_make_model_and_years():
    from bluenile.vehicles import find_spec
    specs = {"vehicles": [{"make": "toyota", "model": "corolla", "years": [2019, 2025], "oil_grade": "0W-20"}]}
    assert find_spec("Toyota", "Corolla Ascent", 2021, specs)["oil_grade"] == "0W-20"
    assert find_spec("toyota", "corolla", None, specs) is not None
    assert find_spec("Toyota", "Corolla", 2010, specs) is None
    assert find_spec("Mazda", "3", 2021, specs) is None


def test_shipped_specs_file_is_valid_and_empty_until_checked():
    from bluenile.vehicles import load_specs
    assert load_specs()["vehicles"] == []
