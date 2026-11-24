"""Builds the shop's public website from the same data the agent uses:
the price list (bluenile/price_list.json) and the shop settings (SHOP_* env vars).

The page is a booking storefront (bluenile/site_template.html): customers add
services to a booking cart, pick a live time slot and confirm. Bookings go to
the API in bluenile/web.py (run web_server.py), which writes them into the
workshop database. Re-run `python build_website.py` after changing prices or
details, so the website and the agent never disagree on a price.

Rules for what goes public:
- Only confirmed prices are shown. Unconfirmed or missing ones say "Price on quote".
- Only `description` and `web_note` are shown; `notes` is internal.
- Missing contact details are left out, and the build lists them as warnings.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from html import escape as e
from pathlib import Path

from .tools import ShopConfig
from .brand import emblem_svg, favicon_data_uri
from .vehicles import load_rules
from .web import fixed_price_cents

TEMPLATE = Path(__file__).with_name("site_template.html")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# The standard service rows on the price list. Customers never pick one: the price is
# chosen from their car's make and model (bluenile/vehicle_classes.json).
TIER_CATEGORY = "Vehicle service"

GROUPS = {  # price list category -> (tab key, tab label)
    "Essential services": ("essentials", "Essentials"),
    "Flushes and fluids": ("fluids", "Flushes & fluids"),
    "Filters, wipers, lights and extras": ("extras", "Filters, wipers & extras"),
}

SHORT_NAMES = {
    "RWC (roadworthy certificate)": "Roadworthy (RWC)",
    "Pre / post purchase inspection": "Pre-purchase inspection",
    "Rotate and balance tyres": "Rotate & balance",
}

DEFAULT_QTY = {"Cheap wipers": 2, "Expensive wipers": 2}

# Suggestions for the "Make" box (customers can still type anything).
COMMON_MAKES = [
    "Toyota", "Mazda", "Hyundai", "Kia", "Ford", "Mitsubishi", "Nissan", "Subaru", "Volkswagen", "Holden",
    "Honda", "Isuzu", "MG", "Suzuki", "BYD", "Tesla", "GWM", "LDV", "Mercedes-Benz", "BMW", "Audi", "Lexus",
    "Jeep", "Volvo", "Land Rover", "Skoda", "Peugeot", "Renault", "Mini", "RAM", "Chery", "SsangYong",
    "Porsche", "Jaguar", "Fiat", "Alfa Romeo", "Cupra", "Citroen", "Chrysler", "Chevrolet", "Genesis", "Polestar",
]


def _dollars(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def web_price(item: dict) -> str | None:
    """The price as shown publicly, or None for 'price on quote'."""
    if not item.get("confirmed"):
        return None
    lo, hi, unit = item.get("price_min"), item.get("price_max"), item.get("unit")
    if lo is None and hi is None:
        return None
    if hi is None:
        text = f"From {_dollars(lo)}"
    elif lo is None or lo == hi:
        text = _dollars(hi)
    else:
        text = f"{_dollars(lo)}–{_dollars(hi)}"
    return f"{text} {unit}" if unit else text


def _hour(h: int) -> str:
    return f"{h % 12 or 12}:00{'am' if h < 12 else 'pm'}"


def opening_hours(cfg: ShopConfig) -> list[tuple[str, str]]:
    """Group consecutive days with the same hours: [("Monday – Saturday", "8:00am – 5:00pm"), ...]."""
    rows: list[tuple[list[int], str]] = []
    for d in range(7):
        hours = f"{_hour(cfg.open_hour)} – {_hour(cfg.close_hour)}" if d in cfg.open_days else "Closed"
        if rows and rows[-1][1] == hours and rows[-1][0][-1] == d - 1:
            rows[-1][0].append(d)
        else:
            rows.append(([d], hours))
    return [(DAY_NAMES[days[0]] if len(days) == 1 else f"{DAY_NAMES[days[0]]} – {DAY_NAMES[days[-1]]}", hours)
            for days, hours in rows]


def _days_open(cfg: ShopConfig) -> str:
    days = sorted(cfg.open_days)
    if days and days == list(range(days[0], days[-1] + 1)):
        return f"Open {DAY_NAMES[days[0]][:3]}–{DAY_NAMES[days[-1]][:3]}"
    return "Open " + ", ".join(DAY_NAMES[d][:3] for d in days)


def suburb_of(address: str) -> str:
    """'746 Beaudesert Road, Rocklea QLD 4106' -> 'Rocklea QLD'."""
    m = re.search(r",\s*([^,]+?\s(?:QLD|NSW|VIC|SA|WA|TAS|ACT|NT))\b", address or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


def missing_details(cfg: ShopConfig) -> list[str]:
    return [label for label, value in [
        ("phone number (SHOP_PHONE)", cfg.phone),
        ("address (SHOP_ADDRESS)", cfg.address),
        ("email (SHOP_EMAIL)", cfg.email),
        ("ABN (SHOP_ABN)", cfg.abn),
    ] if not value]


def _tel(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit() or ch == "+")


def public_service(s: dict) -> dict:
    """Only the fields the public page may see."""
    fixed = fixed_price_cents(s)
    label = web_price(s)
    from_cents = None
    if fixed is None and label and s.get("price_min") is not None:
        from_cents = round(s["price_min"] * 100)
    is_tier = s["category"] == TIER_CATEGORY
    group, group_label = GROUPS.get(s["category"], ("extras", s["category"]))
    return {
        "name": s["name"],
        "display": "Standard service" if is_tier else s["name"],
        "short": SHORT_NAMES.get(s["name"]),
        "group": group,
        "group_label": group_label,
        "description": s.get("description", ""),
        "web_note": s.get("web_note"),
        "fixed_cents": fixed,
        "from_cents": from_cents,
        "price_label": None if fixed is not None else label,
        "unit": s.get("unit"),
        "tier": is_tier,
        "default_qty": DEFAULT_QTY.get(s["name"], 1),
    }


def _faq(cfg: ShopConfig, services: dict) -> list[list[str]]:
    labour = services.get("Diagnostic / labour")
    labour_price = web_price(labour) if labour else None
    faq = []
    if cfg.tax_inclusive:
        faq.append([f"Do your prices include {cfg.tax_name}?", f"Yes. Every price on this site includes {cfg.tax_name}."])
    faq += [
        ["What does “Price on quote” mean?",
         "Some jobs depend on your car. Add them to your booking and we'll call you with the price before we start."],
        ["How is the standard service price worked out?",
         "From your car's make and model. Enter them and you'll see the price for your car straight away."],
        ["How do I get a price for tyres?",
         "Choose your tyre size in the Tyres section and add it to your booking. We'll call you with a fitted price."],
        ["My job isn't listed. Can I still book?",
         "Yes. Add Diagnostic / labour" + (f" ({labour_price})" if labour_price else "")
         + " and tell us what's wrong in the notes, or give us a call."],
        ["Can I change or cancel my booking?",
         f"Yes, just call us{' on ' + cfg.phone if cfg.phone else ''}."],
    ]
    return faq


def render(cfg: ShopConfig, prices: dict, api_base: str | None = None, rego_lookup: bool = False) -> str:
    services = prices["services"]
    by_name = {s["name"]: s for s in services}
    tiers = [s for s in services if s["category"] == TIER_CATEGORY]
    from_cents = min((fixed_price_cents(s) for s in tiers if fixed_price_cents(s) is not None), default=None)
    gst_line = (f"All prices include {cfg.tax_name}." if cfg.tax_inclusive
                else f"{cfg.tax_name} is added to these prices.")
    as_of = date.fromisoformat(prices["as_of"]).strftime("%-d %B %Y") if prices.get("as_of") else ""
    seen_groups = []
    for s in services:
        if s["category"] != TIER_CATEGORY:
            g = GROUPS.get(s["category"], ("extras", s["category"]))
            if g not in seen_groups:
                seen_groups.append(g)

    data = {
        "shop": {
            "name": cfg.name, "phone": cfg.phone, "tel": _tel(cfg.phone), "email": cfg.email,
            "address": cfg.address, "abn": cfg.abn, "website": cfg.website, "gst_line": gst_line, "tax_inclusive": cfg.tax_inclusive,
            "tax_name": cfg.tax_name, "labour_cents": cfg.labor_rate_cents,
            "hours": opening_hours(cfg), "days_open": _days_open(cfg),
            "price_note": f"incl. {cfg.tax_name}" if cfg.tax_inclusive else f"+ {cfg.tax_name}",
            "booking_hours": cfg.booking_hours,
        },
        "vehicle_rules": load_rules(),
        "makes": COMMON_MAKES,
        "rego_lookup": rego_lookup,
        "as_of": as_of,
        "api": (api_base if api_base is not None else os.environ.get("SHOP_BOOKING_API", "")).rstrip("/"),
        "groups": [{"key": k, "label": label} for k, label in seen_groups],
        "services": [public_service(s) for s in services],
        "tyre_suggest": (prices.get("tyres") or {}).get("suggest_with", []),
        "faq": _faq(cfg, by_name),
    }
    # Safe inside <script>: no "</script>" or "<!--" can appear in the JSON.
    data_json = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    suburb = suburb_of(cfg.address)
    description = (f"{cfg.name}{', ' + suburb if suburb else ''}: tyres, wheels and car servicing. Book online"
                   + (f" from {_dollars(from_cents / 100)}" if from_cents else "")
                   + ". Roadworthies, wheel alignments, A/C regas, tyres, flushes and more. "
                   + gst_line)
    html = TEMPLATE.read_text(encoding="utf-8")
    return (html.replace("__TITLE__", e(f"{cfg.name} | Car servicing, tyres & online booking"))
                .replace("__DESCRIPTION__", e(description))
                .replace("__NAME__", e(cfg.name))
                .replace("__EMBLEM__", emblem_svg(46, label=""))
                .replace("__IN_SUBURB__", e(f" in {suburb.rsplit(' ', 1)[0]}") if suburb else "")
                .replace("__FAVICON__", favicon_data_uri())
                .replace("__DATA__", data_json))


def page_data(html: str) -> dict:
    """Read the embedded data back out of a built page (used by tests and checks)."""
    marker = '<script type="application/json" id="shop-data">'
    start = html.index(marker) + len(marker)
    return json.loads(html[start:html.index("</script>", start)])


def build(cfg: ShopConfig | None = None, out_dir: Path = Path("website"),
          api_base: str | None = None) -> tuple[Path, list[str]]:
    cfg = cfg or ShopConfig()
    prices = json.loads(Path(cfg.price_list).read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(render(cfg, prices, api_base), encoding="utf-8")
    return path, missing_details(cfg)
