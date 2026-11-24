"""Talk to the shop agent from the terminal.

    python run_agent.py                 # staff mode: front desk / service writer
    python run_agent.py --mode caller   # phone mode: you play the caller
    python run_agent.py --repo https://github.com/you/shop-site   # mount a repo for bug fixes

Shop data stays in a local SQLite file (shop.db). Files the agent writes to
/mnt/session/outputs/ (websites, patches) are downloaded to ./outputs/<session>/.
Run `python setup_agent.py` once first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import anthropic

from bluenile.agent_config import CALLER_BLOCKED_TOOLS, CONFIRM_TOOLS
from bluenile.db import connect
from bluenile.tools import ShopTools, ToolError

STATE_FILE = Path(".bluenile.json")

MODE_NOTES = {
    "staff": "[Session note] STAFF mode: a shop employee is typing.",
    "caller": "[Session note] CALLER mode: you are on the phone with a customer. "
              "Their words follow; your replies are read aloud. Greet them first.",
}


class SessionRunner:
    def __init__(self, client: anthropic.Anthropic, session_id: str, tools: ShopTools, mode: str):
        self.client = client
        self.session_id = session_id
        self.tools = tools
        self.mode = mode
        self.seen: set[str] = set()

    # ---------------------------------------------------------------- sending
    def send(self, events: list[dict]) -> None:
        self.client.beta.sessions.events.send(session_id=self.session_id, events=events)

    def send_text(self, text: str) -> None:
        self.send([{"type": "user.message", "content": [{"type": "text", "text": text}]}])

    # -------------------------------------------------------------- one turn
    def turn(self, text: str) -> None:
        """Send a message and process events until the agent finishes its turn."""
        sent = False
        for attempt in range(5):
            try:
                with self.client.beta.sessions.events.stream(session_id=self.session_id) as stream:
                    if not sent:  # stream-first: open the stream, then send
                        self.send_text(text)
                        sent = True
                    else:  # reconnect: replay anything missed while disconnected
                        for ev in self.client.beta.sessions.events.list(session_id=self.session_id, order="asc"):
                            if self.handle(ev):
                                return
                    for ev in stream:
                        if self.handle(ev):
                            return
                return  # stream closed by server
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                print(f"\n[stream dropped: {e}; reconnecting]", file=sys.stderr)
                time.sleep(min(2 ** attempt, 10))
        print("\n[could not reconnect; try again]", file=sys.stderr)

    def handle(self, ev) -> bool:
        """Process one event. Returns True when the turn is over."""
        if ev.id in self.seen:
            return False
        self.seen.add(ev.id)

        if ev.type == "agent.message":
            for block in ev.content:
                if block.type == "text":
                    print(block.text, end="", flush=True)
        elif ev.type == "agent.tool_use":
            if getattr(ev, "evaluated_permission", None) == "ask":
                self.confirm_builtin(ev)
            else:
                print(f"\n  · {ev.name}", file=sys.stderr)
        elif ev.type == "agent.mcp_tool_use" and getattr(ev, "evaluated_permission", None) == "ask":
            self.confirm_builtin(ev)
        elif ev.type == "agent.custom_tool_use":
            self.run_custom_tool(ev)
        elif ev.type == "session.error":
            print(f"\n[session error: {getattr(ev, 'error', ev)}]", file=sys.stderr)
        elif ev.type == "session.status_idle":
            # requires_action means the session is waiting on us (tool result or
            # approval) - keep streaming. Anything else ends the turn.
            if ev.stop_reason.type != "requires_action":
                print()
                return True
        elif ev.type == "session.status_terminated":
            print("\n[session ended]")
            return True
        return False

    # ------------------------------------------------------------ tool calls
    def run_custom_tool(self, ev) -> None:
        name, args = ev.name, dict(ev.input or {})
        print(f"\n  · {name} {json.dumps(args)}", file=sys.stderr)
        is_error = False
        if self.mode == "caller" and name in CALLER_BLOCKED_TOOLS:
            result, is_error = f"{name} is staff-only and can't be used during a phone call.", True
        elif name in CONFIRM_TOOLS and not ask_yes_no(f"  Allow {name}({json.dumps(args)})?"):
            result, is_error = "Staff declined this action. Ask them what to do instead.", True
        else:
            try:
                result = self.tools.run(name, args)
            except ToolError as e:
                result, is_error = str(e), True
            except Exception as e:  # keep the session alive on unexpected bugs
                result, is_error = f"Internal error in {name}: {e!r}", True
        self.send([{
            "type": "user.custom_tool_result",
            "custom_tool_use_id": ev.id,
            "content": [{"type": "text", "text": result}],
            "is_error": is_error,
        }])

    def confirm_builtin(self, ev) -> None:
        """A container tool call the server flagged for review (permission policy `auto`)."""
        preview = json.dumps(getattr(ev, "input", {}) or {})[:400]
        allowed = ask_yes_no(f"\n  Agent wants to run {ev.name}: {preview}\n  Allow?")
        self.send([{"type": "user.tool_confirmation", "tool_use_id": ev.id,
                    "result": "allow" if allowed else "deny"}])

    # ---------------------------------------------------------------- outputs
    def download_outputs(self, retries: int = 1) -> None:
        out_dir = Path("outputs") / self.session_id
        files = []
        for attempt in range(retries):  # files are indexed a second or two after idle
            files = list(self.client.beta.files.list(scope_id=self.session_id, betas=["managed-agents-2026-04-01"]))
            if files or attempt == retries - 1:
                break
            time.sleep(1.5)
        for f in files:
            target = out_dir / f.filename
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            self.client.beta.files.download(f.id).write_to_file(target)
            print(f"  ↓ saved {target}")


def ask_yes_no(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
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

    session = client.beta.sessions.create(
        agent={"type": "agent", "id": state["agent_id"], "version": state["agent_version"]},
        environment_id=state["environment_id"],
        title=f"Shop {args.mode} session",
        resources=resources,
    )
    print(f"Session {session.id} ({args.mode} mode). Type 'quit' to exit.")
    print(f"Trace: https://platform.claude.com/workspaces/default/sessions/{session.id}\n")

    runner = SessionRunner(client, session.id, tools, args.mode)
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
        runner.download_outputs()

    runner.download_outputs(retries=3)
    print("Bye. Session history stays in the Console until you delete it.")


if __name__ == "__main__":
    main()
