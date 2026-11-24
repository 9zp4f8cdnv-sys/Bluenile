"""Build the shop's public website into website/index.html.

    SHOP_PHONE="(03) 9000 0000" SHOP_ADDRESS="1 Example St, Suburb VIC 3000" \
    SHOP_EMAIL=bookings@example.com SHOP_ABN="12 345 678 901" python build_website.py

Prices come from bluenile/price_list.json and hours/labour/GST from the SHOP_*
settings, the same data the agent uses. Re-run after any change. The output is
one self-contained HTML file: upload it to any static host (Netlify, Cloudflare
Pages, GitHub Pages, or your existing web host).
"""

from pathlib import Path

from bluenile.website import build

if __name__ == "__main__":
    path, missing = build(out_dir=Path("website"))
    print(f"Built {path}")
    if missing:
        print("Not on the site yet (set these and re-run):")
        for m in missing:
            print(f"  - {m}")
