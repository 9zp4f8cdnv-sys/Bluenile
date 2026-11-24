"""Drives one Managed Agents session: sends messages, runs custom tools,
answers approval requests, and reports the agent's text through callbacks.

Used by the terminal (`run_agent.py`) and the phone line (`bluenile/phone.py`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import anthropic

from .agent_config import CALL_CONTROL_TOOLS, CALLER_BLOCKED_TOOLS, CONFIRM_TOOLS
from .tools import ShopTools, ToolError

MODE_NOTES = {
    "staff": "[Session note] STAFF mode: a shop employee is typing.",
    "caller": "[Session note] CALLER mode: you are on the phone with a customer. "
              "Their words follow; your replies are read aloud. Greet them first.",
}


class SessionRunner:
    def __init__(
        self,
        client: anthropic.Anthropic,
        session_id: str,
        tools: ShopTools,
        mode: str,
        *,
        on_text: Callable[[str], None] = lambda t: None,
        on_status: Callable[[str], None] = lambda s: None,
        confirm: Callable[[str], bool] = lambda prompt: False,
        on_call_control: Callable[[str, dict], str] | None = None,
    ):
        self.client = client
        self.session_id = session_id
        self.tools = tools
        self.mode = mode
        self.on_text = on_text
        self.on_status = on_status
        self.confirm = confirm  # unattended callers leave the default: deny
        self.on_call_control = on_call_control
        self.seen: set[str] = set()

    # ---------------------------------------------------------------- sending
    def send(self, events: list[dict]) -> None:
        self.client.beta.sessions.events.send(session_id=self.session_id, events=events)

    def send_text(self, text: str) -> None:
        self.send([{"type": "user.message", "content": [{"type": "text", "text": text}]}])

    # -------------------------------------------------------------- one turn
    def turn(self, text: str) -> bool:
        """Send a message and process events until the agent finishes its turn.
        Returns False if the stream couldn't be (re)established."""
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
                                return True
                    for ev in stream:
                        if self.handle(ev):
                            return True
                return True  # stream closed by server
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                self.on_status(f"stream dropped: {e}; reconnecting")
                time.sleep(min(2 ** attempt, 10))
        self.on_status("could not reconnect")
        return False

    def handle(self, ev) -> bool:
        """Process one event. Returns True when the turn is over."""
        if ev.id in self.seen:
            return False
        self.seen.add(ev.id)

        if ev.type == "agent.message":
            for block in ev.content:
                if block.type == "text":
                    self.on_text(block.text)
        elif ev.type in ("agent.tool_use", "agent.mcp_tool_use"):
            if getattr(ev, "evaluated_permission", None) == "ask":
                self.confirm_builtin(ev)
            else:
                self.on_status(ev.name)
        elif ev.type == "agent.custom_tool_use":
            self.run_custom_tool(ev)
        elif ev.type == "session.error":
            self.on_status(f"session error: {getattr(ev, 'error', ev)}")
        elif ev.type == "session.status_idle":
            # requires_action means the session is waiting on us (tool result or
            # approval) - keep streaming. Anything else ends the turn.
            return ev.stop_reason.type != "requires_action"
        elif ev.type == "session.status_terminated":
            self.on_status("session ended")
            return True
        return False

    # ------------------------------------------------------------ tool calls
    def execute_tool(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a custom tool under this session's policy. Returns (text, is_error)."""
        if name in CALL_CONTROL_TOOLS:
            if self.on_call_control is None:
                return "No phone call is connected in this session (simulated).", False
            return self.on_call_control(name, args), False
        if self.mode == "caller" and name in CALLER_BLOCKED_TOOLS:
            return f"{name} is staff-only and can't be used during a phone call.", True
        if name in CONFIRM_TOOLS and not self.confirm(f"Allow {name}({json.dumps(args)})?"):
            return ("Staff didn't approve this action. If you're on a call, log a follow_up "
                    "so staff can handle it."), True
        try:
            return self.tools.run(name, args), False
        except ToolError as e:
            return str(e), True
        except Exception as e:  # keep the session alive on unexpected bugs
            return f"Internal error in {name}: {e!r}", True

    def run_custom_tool(self, ev) -> None:
        name, args = ev.name, dict(ev.input or {})
        self.on_status(f"{name} {json.dumps(args)}")
        result, is_error = self.execute_tool(name, args)
        self.send([{
            "type": "user.custom_tool_result",
            "custom_tool_use_id": ev.id,
            "content": [{"type": "text", "text": result}],
            "is_error": is_error,
        }])

    def confirm_builtin(self, ev) -> None:
        """A container tool call the server flagged for review (permission policy `auto`)."""
        preview = json.dumps(getattr(ev, "input", {}) or {})[:400]
        allowed = self.confirm(f"Agent wants to run {ev.name}: {preview}\nAllow?")
        self.send([{"type": "user.tool_confirmation", "tool_use_id": ev.id,
                    "result": "allow" if allowed else "deny"}])

    # ---------------------------------------------------------------- outputs
    def download_outputs(self, out_root: Path = Path("outputs"), retries: int = 1) -> list[Path]:
        out_dir = out_root / self.session_id
        files = []
        for attempt in range(retries):  # files are indexed a second or two after idle
            files = list(self.client.beta.files.list(scope_id=self.session_id, betas=["managed-agents-2026-04-01"]))
            if files or attempt == retries - 1:
                break
            time.sleep(1.5)
        saved = []
        for f in files:
            target = out_dir / f.filename
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            self.client.beta.files.download(f.id).write_to_file(target)
            saved.append(target)
        return saved


def create_session(client: anthropic.Anthropic, state: dict, title: str, resources: list | None = None):
    return client.beta.sessions.create(
        agent={"type": "agent", "id": state["agent_id"], "version": state["agent_version"]},
        environment_id=state["environment_id"],
        title=title,
        resources=resources or [],
    )
