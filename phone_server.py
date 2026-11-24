"""Run the shop's phone line (Twilio voice webhooks).

    export TWILIO_AUTH_TOKEN=...          # Twilio Console > Account info
    export PUBLIC_BASE_URL=https://your-host.example.com
    export SHOP_TRANSFER_NUMBER=+15551234567   # optional: where "talk to a person" goes
    python phone_server.py                # listens on :8080 (PORT to change)

Point your Twilio number's "A call comes in" webhook at
$PUBLIC_BASE_URL/voice/incoming and "Call status changes" at
$PUBLIC_BASE_URL/voice/status (both HTTP POST). See README.md.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import anthropic

from bluenile.db import connect
from bluenile.phone import Call, PhoneConfig, create_app
from bluenile.session import SessionRunner, create_session
from bluenile.tools import ShopTools

STATE_FILE = Path(".bluenile.json")


def build_app():
    if not STATE_FILE.exists():
        sys.exit("Run `python setup_agent.py` first.")
    state = json.loads(STATE_FILE.read_text())
    client = anthropic.Anthropic()
    cfg = PhoneConfig()
    log = logging.getLogger("bluenile.phone")

    def runner_factory(call: Call) -> SessionRunner:
        session = create_session(client, state, f"Phone call {call.from_number or call.sid}")
        log.info("call %s -> session %s", call.sid, session.id)
        return SessionRunner(
            client, session.id,
            # Each call gets its own connection; only one thread uses it at a time.
            ShopTools(connect(check_same_thread=False)),
            "caller",
            on_text=call.on_text,
            on_status=lambda s: log.info("[%s] %s", call.sid, s),
            # Nobody is at the terminal during a call: anything that needs
            # staff approval is declined, and the agent logs a follow-up instead.
            confirm=lambda prompt: False,
            on_call_control=call.call_control(cfg.transfer_number),
        )

    return create_app(runner_factory, cfg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = build_app()
    # Flask's built-in server is fine for one shop's call volume; use a WSGI
    # server with threads (e.g. `gunicorn -w 1 --threads 16 'phone_server:build_app()'`)
    # for anything bigger. Keep one worker process: call state lives in memory.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
