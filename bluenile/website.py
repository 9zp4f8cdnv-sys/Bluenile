"""Builds the shop's public website from the same data the agent uses:
the price list (bluenile/price_list.json) and the shop settings (SHOP_* env vars).

Re-run `python build_website.py` after changing prices or details, so the
website and the agent never disagree on a price.

Rules for what goes public:
- Only confirmed prices are shown. Unconfirmed or missing ones say "Call for a quote".
- Only `web_note` is shown; `notes` is internal (it records what was hard to read).
- Missing contact details are left out, and the build lists them as warnings.
"""

from __future__ import annotations

import json
from datetime import date
from html import escape as e
from pathlib import Path

from .tools import ShopConfig

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Public names for the service tiers on the price list.
TIER_LABELS = {
    "Standard service - normal cars": ("Normal cars", "Hatchbacks, sedans and small SUVs"),
    "Standard service - large cars and EU": ("Large & European cars", "Large sedans, wagons, SUVs and European makes"),
    "Standard service - very big cars": ("Very big cars", "4WDs, utes, vans and people movers"),
}

CATEGORY_INTROS = {
    "Essential services": "Everyday jobs that keep your car safe and on the road.",
    "Flushes and fluids": "Fresh fluids protect your brakes, steering, cooling system and transmission.",
    "Filters, wipers, lights and extras": "Quick add-ons we can do while your car is in.",
}


def _dollars(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def web_price(item: dict) -> str | None:
    """The price as shown publicly, or None for 'call for a quote'."""
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
    out = []
    for days, hours in rows:
        label = DAY_NAMES[days[0]] if len(days) == 1 else f"{DAY_NAMES[days[0]]} – {DAY_NAMES[days[-1]]}"
        out.append((label, hours))
    return out


def missing_details(cfg: ShopConfig) -> list[str]:
    return [label for label, value in [
        ("phone number (SHOP_PHONE)", cfg.phone),
        ("address (SHOP_ADDRESS)", cfg.address),
        ("email (SHOP_EMAIL)", cfg.email),
        ("ABN (SHOP_ABN)", cfg.abn),
    ] if not value]


def _tel(phone: str) -> str:
    return "tel:" + "".join(ch for ch in phone if ch.isdigit() or ch == "+")


def render(cfg: ShopConfig, prices: dict) -> str:
    services = prices["services"]
    tiers = [s for s in services if s["name"] in TIER_LABELS]
    from_price = min((s["price_min"] for s in tiers if s.get("confirmed") and s.get("price_min")), default=None)
    gst = cfg.tax_name if cfg.tax_inclusive else None
    as_of = date.fromisoformat(prices["as_of"]).strftime("%-d %B %Y") if prices.get("as_of") else ""

    call_btn = (f'<a class="btn btn-primary" href="{_tel(cfg.phone)}">Call {e(cfg.phone)}</a>'
                if cfg.phone else "")
    top_call = f'<a class="top-call" href="{_tel(cfg.phone)}">{e(cfg.phone)}</a>' if cfg.phone else ""

    # Service tier cards
    cards = []
    for s in tiers:
        title, blurb = TIER_LABELS[s["name"]]
        price = web_price(s)
        cards.append(f"""
        <div class="tier">
          <h3>{e(title)}</h3>
          <p class="tier-blurb">{e(blurb)}</p>
          <p class="tier-price">{e(price) if price else "Call for a quote"}</p>
        </div>""")

    # Price tables by category, in list order
    categories: dict[str, list[dict]] = {}
    for s in services:
        if s["name"] in TIER_LABELS:
            continue
        categories.setdefault(s["category"], []).append(s)
    tables = []
    for cat, items in categories.items():
        rows = []
        for s in items:
            price = web_price(s)
            note = f'<span class="note">{e(s["web_note"])}</span>' if s.get("web_note") else ""
            price_html = e(price) if price else '<span class="quote">Call for a quote</span>'
            rows.append(f'<tr><td>{e(s["name"])}{note}</td><td class="price">{price_html}</td></tr>')
        intro = CATEGORY_INTROS.get(cat, "")
        tables.append(f"""
      <div class="price-group">
        <h3>{e(cat)}</h3>
        {f'<p class="muted">{e(intro)}</p>' if intro else ''}
        <table><tbody>{''.join(rows)}</tbody></table>
      </div>""")

    hours_rows = "".join(f"<tr><th>{e(d)}</th><td>{e(h)}</td></tr>" for d, h in opening_hours(cfg))
    contact = []
    if cfg.phone:
        contact.append(f'<li><span>Phone</span><a href="{_tel(cfg.phone)}">{e(cfg.phone)}</a></li>')
    if cfg.email:
        contact.append(f'<li><span>Email</span><a href="mailto:{e(cfg.email)}">{e(cfg.email)}</a></li>')
    if cfg.address:
        contact.append(f'<li><span>Address</span>{e(cfg.address)}</li>')
    contact_html = "".join(contact) or '<li class="muted">Contact details coming soon.</li>'
    map_link = (f'<a class="btn btn-ghost" href="https://www.google.com/maps/search/?api=1&amp;query='
                f'{e(cfg.address.replace(" ", "+"))}">Get directions</a>') if cfg.address else ""
    labour = _dollars(cfg.labor_rate_cents / 100)
    gst_line = f"All prices include {e(gst)}." if gst else f"{e(cfg.tax_name)} is added to these prices."

    return f"""<!doctype html>
<html lang="en-AU">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(cfg.name)} | Car servicing and repairs</title>
<meta name="description" content="{e(cfg.name)}: car servicing{f' from {_dollars(from_price)}' if from_price else ''}, roadworthy certificates, wheel alignments, A/C regas, flushes and more. Clear prices{', GST included' if gst else ''}.">
<style>
:root {{
  --bg: #f6f7f9; --surface: #ffffff; --ink: #14202e; --muted: #5a6776; --line: #e2e6eb;
  --brand: #0d4f8b; --brand-ink: #ffffff; --accent: #f2a516; --accent-ink: #1b1300; --hero: #0b2a47;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f151c; --surface: #17202a; --ink: #e8edf2; --muted: #9aa8b6; --line: #273340;
    --brand: #4b9be0; --brand-ink: #06121e; --accent: #f2b53a; --accent-ink: #1b1300; --hero: #0a1d31;
  }}
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{ margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
a {{ color: var(--brand); }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding: 0 20px; }}
header.top {{ position: sticky; top: 0; z-index: 10; background: var(--surface); border-bottom: 1px solid var(--line); }}
.top .wrap {{ display: flex; align-items: center; gap: 16px; height: 60px; }}
.logo {{ font-weight: 800; font-size: 1.1rem; color: var(--ink); text-decoration: none; letter-spacing: -0.01em; }}
.logo span {{ color: var(--brand); }}
nav {{ display: flex; gap: 18px; margin-left: auto; }}
nav a {{ color: var(--muted); text-decoration: none; font-weight: 500; }}
nav a:hover {{ color: var(--ink); }}
.top-call {{ font-weight: 700; text-decoration: none; white-space: nowrap; }}
.hero {{ background: var(--hero); color: #fff; padding: 72px 0 64px; }}
.hero h1 {{ font-size: clamp(2rem, 5vw, 3.1rem); line-height: 1.1; margin: 0 0 16px; letter-spacing: -0.02em; max-width: 16ch; }}
.hero p {{ font-size: 1.15rem; color: #c9d6e3; margin: 0 0 28px; max-width: 46ch; }}
.hero .from {{ color: var(--accent); font-weight: 700; }}
.btns {{ display: flex; flex-wrap: wrap; gap: 12px; }}
.btn {{ display: inline-block; padding: 12px 20px; border-radius: 8px; font-weight: 700; text-decoration: none; }}
.btn-primary {{ background: var(--accent); color: var(--accent-ink); }}
.btn-ghost {{ border: 1.5px solid currentColor; color: inherit; }}
.hero .btn-ghost {{ color: #fff; }}
section {{ padding: 56px 0; }}
section h2 {{ font-size: 1.75rem; margin: 0 0 8px; letter-spacing: -0.01em; }}
.muted {{ color: var(--muted); margin: 0 0 20px; }}
.tiers {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 24px; }}
.tier {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 22px; }}
.tier h3 {{ margin: 0 0 4px; font-size: 1.15rem; }}
.tier-blurb {{ color: var(--muted); margin: 0 0 16px; font-size: 0.95rem; }}
.tier-price {{ font-size: 2rem; font-weight: 800; margin: 0; color: var(--brand); }}
.price-groups {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
.price-group {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 22px; }}
.price-group h3 {{ margin: 0 0 4px; }}
table {{ width: 100%; border-collapse: collapse; }}
td, th {{ padding: 10px 0; border-top: 1px solid var(--line); vertical-align: top; text-align: left; }}
td.price {{ text-align: right; font-weight: 700; padding-left: 12px; width: 42%; }}
.note {{ display: block; color: var(--muted); font-size: 0.85rem; }}
.quote {{ color: var(--muted); font-weight: 500; white-space: nowrap; }}
.fine {{ color: var(--muted); font-size: 0.9rem; margin-top: 20px; }}
.visit {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }}
.card {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 22px; }}
.card h3 {{ margin: 0 0 12px; }}
.hours th {{ font-weight: 500; color: var(--muted); padding-right: 12px; }}
.contact {{ list-style: none; margin: 0 0 16px; padding: 0; }}
.contact li {{ padding: 10px 0; border-top: 1px solid var(--line); display: flex; gap: 12px; }}
.contact li span {{ color: var(--muted); min-width: 70px; }}
.cta {{ background: var(--brand); color: var(--brand-ink); text-align: center; }}
.cta h2 {{ margin-bottom: 8px; }}
.cta p {{ margin: 0 0 22px; opacity: 0.9; }}
footer {{ padding: 28px 0; color: var(--muted); font-size: 0.9rem; }}
footer .wrap {{ display: flex; flex-wrap: wrap; gap: 8px 24px; justify-content: space-between; }}
@media (max-width: 720px) {{
  nav {{ display: none; }}
  .top-call {{ margin-left: auto; }}
  .hero {{ padding: 48px 0 44px; }}
  section {{ padding: 40px 0; }}
}}
</style>
</head>
<body>
<header class="top">
  <div class="wrap">
    <a class="logo" href="#">{_logo(cfg.name)}</a>
    <nav><a href="#servicing">Servicing</a><a href="#prices">Prices</a><a href="#visit">Hours &amp; contact</a></nav>
    {top_call}
  </div>
</header>

<main>
<section class="hero">
  <div class="wrap">
    <h1>Car servicing and repairs, priced up front.</h1>
    <p>{f'Standard service from <span class="from">{_dollars(from_price)}</span>. ' if from_price else ''}{gst_line}
    Roadworthies, wheel alignments, A/C regas, flushes and more.</p>
    <div class="btns">
      {call_btn}
      <a class="btn btn-ghost" href="#prices">See all prices</a>
    </div>
  </div>
</section>

<section id="servicing">
  <div class="wrap">
    <h2>Standard service</h2>
    <p class="muted">One fixed price, based on the size of your car.</p>
    <div class="tiers">{''.join(cards)}
    </div>
  </div>
</section>

<section id="prices">
  <div class="wrap">
    <h2>Prices</h2>
    <p class="muted">{gst_line} Labour and diagnostics are {labour} per hour.</p>
    <div class="price-groups">{''.join(tables)}
    </div>
    <p class="fine">Prices current as of {e(as_of)}. Some jobs vary by vehicle; we'll confirm the price when you book.</p>
  </div>
</section>

<section id="visit">
  <div class="wrap">
    <h2>Hours &amp; contact</h2>
    <div class="visit">
      <div class="card">
        <h3>Opening hours</h3>
        <table class="hours"><tbody>{hours_rows}</tbody></table>
      </div>
      <div class="card">
        <h3>Get in touch</h3>
        <ul class="contact">{contact_html}</ul>
        <div class="btns">{map_link}</div>
      </div>
    </div>
  </div>
</section>

<section class="cta">
  <div class="wrap">
    <h2>Book your car in</h2>
    <p>{'Call us and we\'ll find a time that suits.' if cfg.phone else 'Online booking coming soon.'}</p>
    <div class="btns" style="justify-content:center">{call_btn}</div>
  </div>
</section>
</main>

<footer>
  <div class="wrap">
    <span>&copy; {date.today().year} {e(cfg.name)}{f' · ABN {e(cfg.abn)}' if cfg.abn else ''}</span>
    <span>{gst_line}</span>
  </div>
</footer>
</body>
</html>
"""


def _logo(name: str) -> str:
    """Bold the first word in the brand colour: 'Bluenile Auto Repair' -> <span>Bluenile</span> Auto Repair."""
    first, _, rest = name.partition(" ")
    return f"<span>{e(first)}</span> {e(rest)}".strip()


def build(cfg: ShopConfig | None = None, out_dir: Path = Path("website")) -> tuple[Path, list[str]]:
    cfg = cfg or ShopConfig()
    prices = json.loads(Path(cfg.price_list).read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(render(cfg, prices), encoding="utf-8")
    return path, missing_details(cfg)
