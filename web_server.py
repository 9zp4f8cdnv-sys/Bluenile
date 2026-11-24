"""Host the shop website with live online booking.

    python build_website.py      # build website/index.html first
    python web_server.py         # http://localhost:8000 (PORT to change)

Online bookings go straight into shop.db (or $BLUENILE_DB): customer, vehicle,
appointment and a work order with the priced items. Put it behind HTTPS
(any reverse proxy or host such as Render, Fly.io or a VPS with Caddy).
"""

import os
from pathlib import Path

from bluenile.db import connect
from bluenile.tools import ShopTools
from bluenile.web import create_web_app

app = create_web_app(lambda: ShopTools(connect()), Path("website"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)
