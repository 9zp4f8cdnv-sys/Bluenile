"""Talk to the shop agent from the terminal.

    python run_agent.py                 # staff mode: front desk / service writer
    python run_agent.py --mode caller   # practice a phone call by typing
    python run_agent.py --repo https://github.com/you/shop-site   # mount a repo for bug fixes

Shop data stays in a local SQLite file (shop.db). Files the agent writes to
/mnt/session/outputs/ (websites, patches) are downloaded to ./outputs/<session>/.
Run `python setup_agent.py` once first. For real phone calls, see phone_server.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic

from bluenile.db import connect
from bluenile.session import MODE_NOTES, SessionRunner, create_session
from bluenile.tools import ShopTools

STATE_FILE = Path(".bluenile.json")


def ask_yes_no(prompt: str) -> bool:
    try:
        return input(f"\n  {prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["staff", "caller"], default="staff")
    parser.add_argument("--repo", help="GitHub repo URL to mount at /workspace/repo (needs GITHUB_TOKEN)")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--db", help="SQLite file (default: shop.db or $BLUENILE_DB)")
    args = parser.parse_args()

    if not STATE_FILE.exists():
        sys.exit("Run `python setup_agent.py` first.")
    state = json.loads(STATE_FILE.read_text())

    client = anthropic.Anthropic()
    tools = ShopTools(connect(args.db))

    resources = []
    if args.repo:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("--repo needs a GITHUB_TOKEN environment variable.")
        resources.append({
            "type": "github_repository", "url": args.repo, "mount_path": "/workspace/repo",
            "authorization_token": token, "checkout": {"type": "branch", "name": args.branch},
        })

    session = create_session(client, state, f"Shop {args.mode} session", resources)
    print(f"Session {session.id} ({args.mode} mode). Type 'quit' to exit.")
    print(f"Trace: https://platform.claude.com/workspaces/default/sessions/{session.id}\n")

    runner = SessionRunner(
        client, session.id, tools, args.mode,
        on_text=lambda t: print(t, end="", flush=True),
        on_status=lambda s: print(f"\n  · {s}", file=sys.stderr),
        confirm=ask_yes_no,
    )
    first = True
    while True:
        try:
            text = input("caller> " if args.mode == "caller" else "you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if not text:
            continue
        if first:
            text = f"{MODE_NOTES[args.mode]}\n\n{text}"
            first = False
        print("agent> ", end="", flush=True)
        runner.turn(text)
        print()
        for path in runner.download_outputs():
            print(f"  ↓ saved {path}")

    for path in runner.download_outputs(retries=3):
        print(f"  ↓ saved {path}")
    print("Bye. Session history stays in the Console until you delete it.")


if __name__ == "__main__":
    main()
