"""Host the shop website with live online booking, and the staff app.

    python build_website.py      # build website/index.html first
    STAFF_PASSWORD='choose-one' python web_server.py   # http://localhost:8000 (PORT to change)

In production (Render, see render.yaml) it runs under gunicorn:
    gunicorn --workers 1 --threads 8 web_server:app

- /            the public website; online bookings go straight into shop.db
               (or $BLUENILE_DB): customer, vehicle, appointment, work order,
               and a booking in the staff app.
- /staff/      the staff app (jobs, bookings, stock, cars), shared by every
               phone. Needs STAFF_PASSWORD; it's off without one.
- /healthz     for the host's health check.

Keep it to one worker process: the staff sign-in limiter and booking rate
limit live in memory. Behind a host's proxy set TRUST_PROXY=1 so each
customer's own address is used for rate limiting. Staff sign-in cookies are
HTTPS-only; for testing over plain http on another machine, set
STAFF_INSECURE_COOKIES=1.
"""

import os
from pathlib import Path

from werkzeug.middleware.proxy_fix import ProxyFix

from bluenile.db import DEFAULT_DB_PATH, connect
from bluenile.staff import register_staff_app
from bluenile.tools import ShopConfig, ShopTools
from bluenile.web import create_web_app

DB = Path(DEFAULT_DB_PATH)
DB.parent.mkdir(parents=True, exist_ok=True)

app = create_web_app(lambda: ShopTools(connect(DB)), Path("website"))
register_staff_app(app, lambda: connect(DB), ShopConfig, DB,
                   secure_cookies=os.environ.get("STAFF_INSECURE_COOKIES") != "1")

if os.environ.get("TRUST_PROXY") == "1":
    # The host's load balancer sets X-Forwarded-For / -Proto; trust exactly one hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)
