# Bluenile

An AI assistant for a mechanic shop. It answers the phone, books jobs, runs work
orders through to invoices and payments, and handles technical work such as
building the shop's website or fixing bugs in it.

It is built on [Claude Managed Agents](https://platform.claude.com/docs) (beta):
Anthropic runs the agent loop and a per-session container where the agent can
write code. Shop records stay in a local SQLite file on your machine; the agent
reads and changes them only through the tools in `bluenile/tools.py`.

## What it can do

| Area | Tools |
| --- | --- |
| Phone calls | A real Twilio phone line (`phone_server.py`), `log_call`, `list_calls` (callbacks needed), `end_call`, `transfer_to_staff` |
| Price quotes | `lookup_prices` (your service price list, with unconfirmed prices flagged) |
| Customers & vehicles | `find_customer` (name, phone, plate, VIN), `save_customer`, `add_vehicle` |
| Scheduling | `check_availability` (hours × bays), `book_appointment`, `cancel_appointment`, `list_appointments` |
| Work orders | `create_work_order`, `add_work_order_line` (labor/parts/fees), `update_work_order`, `get_work_order`, `list_work_orders` |
| Invoices & payments | `create_invoice` (GST-inclusive totals, printable tax invoice in `invoices/`), `record_payment`, `get_invoice` |
| Website & online booking | `build_website.py` + `web_server.py`: service shop, tyre finder, cart and live booking into the workshop system |
| Websites, bug fixes, new agents | The container's bash and file tools; results land in `outputs/<session>/` |

Guardrails:
- `create_invoice`, `record_payment` and `cancel_appointment` ask a staff member
  `[y/N]` in the terminal before running.
- In caller mode the agent can't invoice, take payments or edit work orders.
- Invoiced work orders are locked; payments can't exceed the balance due.
- Container commands are checked by the server (`auto` permission policy); any it
  flags are shown to you for approval.
- Web search/fetch are off, and the container can reach package registries only.

## Prices

The agent quotes only from `bluenile/price_list.json` (via `lookup_prices`); it
never makes up prices. Entries with `"confirmed": false` (unclear in the source
photo) are quoted as a guide only, with a follow-up logged for a service writer
to confirm. Anything not on the list gets a follow-up instead of a quote.

To change a price, edit the JSON file (no setup re-run needed) and set
`"confirmed": true` once it's checked. To use a file elsewhere, set `SHOP_PRICE_LIST`.

## Website and online booking

The public website is a booking storefront built from the same data the agent
uses (the price list and `SHOP_*` settings), so the two always agree on prices.

- **Shop-style browsing:** service cards with prices, category tabs and
  search, a car-size picker for the standard service, and a tyre finder by size.
- **Booking cart:** customers add services, see the total incl. GST (and the
  GST amount), then check out: car → live time slot → contact details.
- **Straight into the workshop:** a confirmed booking creates (or matches, by
  phone and rego) the customer and vehicle, books a bay, and opens a work order
  with every fixed-price item on it. Ranges, unconfirmed prices and tyres are
  listed in the work order notes as "Quote needed" so staff call the customer.
- **Safe by default:** the server re-prices everything from the price list
  (the browser only sends service names), checks slots against opening hours
  and free bays, rejects bookings less than an hour away or more than 60 days
  out, and has a spam trap and a per-connection rate limit.
- Only confirmed prices are published; only `description` and `web_note` from
  the price list are public (`notes` stays internal). `hours` in the price list
  sets how long a booking reserves a bay (default 1).

```bash
python build_website.py   # builds website/index.html; lists any missing contact details
python web_server.py      # serves the site + booking API on :8000 (PORT to change)
```

Run `web_server.py` behind HTTPS on any small server. To host the page
somewhere else (Netlify, your current host) and the API separately, set
`SHOP_BOOKING_API=https://your-api-host` before building. If the booking API
can't be reached, the checkout tells customers to call instead.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # or run `ant auth login`
python setup_agent.py                   # once: creates the environment + agent, saves IDs to .bluenile.json
```

Shop settings come from environment variables:

| Variable | Default |
| --- | --- |
| `SHOP_NAME`, `SHOP_ADDRESS`, `SHOP_PHONE`, `SHOP_EMAIL`, `SHOP_ABN` | `Bluenile Auto Repair`, empty, empty, empty, empty |
| `SHOP_LABOR_RATE` (dollars/hour, incl. GST) | `130` |
| `SHOP_TAX_NAME`, `SHOP_TAX_RATE` | `GST`, `0.10` |
| `SHOP_TAX_INCLUSIVE` (`1`: prices include GST; `0`: tax added on top) | `1` |
| `SHOP_TAX_LABOR` (`0` to exempt labour from tax) | `1` |
| `SHOP_OPEN_HOUR`, `SHOP_CLOSE_HOUR`, `SHOP_BAYS` | `8`, `17`, `3` (open Mon–Sat) |
| `BLUENILE_DB`, `SHOP_INVOICE_DIR` | `shop.db`, `invoices` |

To change the prompt or tools, edit `bluenile/agent_config.py` / `bluenile/tools.py`
and run `python setup_agent.py` again; it publishes a new agent version.

## Use

```bash
python run_agent.py                     # staff: "Book Dana's Civic for brakes Tuesday morning"
python run_agent.py --mode caller       # you play a customer calling in
GITHUB_TOKEN=... python run_agent.py --repo https://github.com/you/shop-site   # bug fixing
```

Example staff requests:
- "Who still needs a callback?"
- "Open a work order for the Civic: grinding when braking. Add 1.5 h labour and a $64.99 pad set, then invoice it."
- "Record a $251.49 card payment on invoice 1."
- "Build a one-page website for the shop with hours, services and our phone number."

## Costs and data

Managed Agents is in beta. Sessions bill model tokens plus session runtime, and
session transcripts are kept in your Anthropic Console until you delete them;
anything a tool returns (customer names, phone numbers) is part of that
transcript. The model is `claude-opus-5-5`.

## Phone line (Twilio)

`phone_server.py` answers real calls on a Twilio number. Twilio turns the
caller's speech into text and reads the agent's replies aloud, so no other
speech service is needed. Each call gets its own agent session.

**What callers get:** an instant greeting; the agent looks them up by caller ID,
books or checks on work, and logs the call with a follow-up when staff need to
call back. Asking for a person (or anything the agent can't handle) transfers
the call to `SHOP_TRANSFER_NUMBER`. If the agent needs more than a few seconds,
the caller hears "One moment please." If a caller hangs up first, the agent
still logs the call.

**What callers can't do:** invoice, take payments, edit work orders, or cancel
appointments. The agent logs a follow-up for staff instead.

### Setup

1. Buy a voice-capable number in the [Twilio Console](https://console.twilio.com).
2. Run the server where Twilio can reach it over HTTPS (a small VPS, or
   `ngrok http 8080` while testing):
   ```bash
   export TWILIO_AUTH_TOKEN=...               # Console > Account info; used to verify requests
   export PUBLIC_BASE_URL=https://your-host   # the exact public URL Twilio calls
   export SHOP_TRANSFER_NUMBER=+15551234567   # optional: the shop's desk phone
   python phone_server.py                     # port 8080; set PORT to change
   ```
3. On the number's configuration page in Twilio, under Voice:
   - **A call comes in** → Webhook, `https://your-host/voice/incoming`, HTTP POST
   - **Call status changes** → `https://your-host/voice/status`, HTTP POST
4. Call the number.

Optional settings: `TWILIO_VOICE` (default `Polly.Joanna-Neural`) and
`TWILIO_LANGUAGE` (default `en-US`).

Notes:
- Requests without a valid Twilio signature are rejected (403). If every call
  fails with 403, `PUBLIC_BASE_URL` doesn't match the URL set in Twilio.
- Call state is kept in memory: run one server process (threads are fine), and a
  restart drops calls in progress.
- Twilio bills per minute for the call and for speech recognition, on top of
  the agent's session costs. Calls are transcribed by Twilio and the transcript
  is stored in the agent session's history.
- `python run_agent.py --mode caller` is still the quickest way to try out
  phone conversations by typing.
- After pulling this change, run `python setup_agent.py` again to publish the
  new `end_call` and `transfer_to_staff` tools to your agent.

## Tests

```bash
python -m pytest -q
```

The tests cover the shop tools and the event handling without calling the API.
