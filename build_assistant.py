"""Build the Blue Nile shop assistant page (assistant/blue-nile-assistant.html).

The assistant is a page published in Claude: staff chat with it to book jobs,
look up prices, log calls, make GST tax invoices and record payments. It asks
Claude through the signed-in person's own Claude account (no API key) and
keeps its records with the page. It uses the same price list, vehicle rules
and shop details as the website and the agent, so re-run this after changing
any of them, then republish the page.
"""

import json
from datetime import date
from html import escape
from pathlib import Path

from bluenile.brand import emblem_svg
from bluenile.tools import ShopConfig, setting
from bluenile.vehicles import load_rules

TEMPLATE = Path("bluenile/assistant_template.html")
OUT = Path("assistant/blue-nile-assistant.html")


def build(cfg: ShopConfig | None = None) -> Path:
    cfg = cfg or ShopConfig()
    prices = json.loads(Path(cfg.price_list).read_text(encoding="utf-8"))
    data = {
        "shop": {
            "name": cfg.name, "address": cfg.address, "phone": cfg.phone, "abn": cfg.abn, "email": cfg.email,
            "website": cfg.website, "open_days": sorted(cfg.open_days), "open_hour": cfg.open_hour,
            "close_hour": cfg.close_hour, "bays": cfg.bays, "booking_hours": cfg.booking_hours,
            "first_invoice_number": int(setting("SHOP_FIRST_INVOICE_NUMBER", "1")),
            "price_list_date": date.fromisoformat(prices["as_of"]).strftime("%-d %B %Y"),
        },
        "services": [{k: s.get(k) for k in ("name", "category", "description", "price_min", "price_max", "unit", "confirmed")}
                     for s in prices["services"]],
        "vehicle_rules": load_rules(),
        "emblem_invoice": emblem_svg(90),
    }
    page = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"))
            .replace("__EMBLEM__", emblem_svg(44, label="")))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    return OUT


if __name__ == "__main__":
    print(f"Built {build()}")
