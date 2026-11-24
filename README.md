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
| Phone calls | `log_call`, `list_calls` (callbacks needed), plus caller mode with a phone-friendly voice |
| Customers & vehicles | `find_customer` (name, phone, plate, VIN), `save_customer`, `add_vehicle` |
| Scheduling | `check_availability` (hours × bays), `book_appointment`, `cancel_appointment`, `list_appointments` |
| Work orders | `create_work_order`, `add_work_order_line` (labor/parts/fees), `update_work_order`, `get_work_order`, `list_work_orders` |
| Invoices & payments | `create_invoice` (tax, totals, printable HTML in `invoices/`), `record_payment`, `get_invoice` |
| Websites, bug fixes, new agents | The container's bash and file tools; results land in `outputs/<session>/` |

Guardrails:
- `create_invoice`, `record_payment` and `cancel_appointment` ask a staff member
  `[y/N]` in the terminal before running.
- In caller mode the agent can't invoice, take payments or edit work orders.
- Invoiced work orders are locked; payments can't exceed the balance due.
- Container commands are checked by the server (`auto` permission policy); any it
  flags are shown to you for approval.
- Web search/fetch are off, and the container can reach package registries only.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # or run `ant auth login`
python setup_agent.py                   # once: creates the environment + agent, saves IDs to .bluenile.json
```

Shop settings come from environment variables:

| Variable | Default |
| --- | --- |
| `SHOP_NAME`, `SHOP_ADDRESS`, `SHOP_PHONE` | `Bluenile Auto Repair`, empty, empty |
| `SHOP_LABOR_RATE` (dollars/hour) | `120` |
| `SHOP_TAX_RATE`, `SHOP_TAX_LABOR` (`1` to tax labor) | `0.0825`, `0` |
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
- "Open a work order for the Civic: grinding when braking. Add 1.5 h labor and a $64.99 pad set, then invoice it."
- "Record a $251.49 card payment on invoice 1."
- "Build a one-page website for the shop with hours, services and our phone number."

## Costs and data

Managed Agents is in beta. Sessions bill model tokens plus session runtime, and
session transcripts are kept in your Anthropic Console until you delete them;
anything a tool returns (customer names, phone numbers) is part of that
transcript. The model is `claude-opus-5-5`.

## Real phone lines

Caller mode is text: you type what the caller says. To put it on a real number,
connect a telephony provider's speech-to-text and text-to-speech to
`SessionRunner.turn()` in `run_agent.py`, one session per call.

## Tests

```bash
python -m pytest -q
```

The tests cover the shop tools and the event handling without calling the API.
