"""One-time setup (and later updates) for the Managed Agents environment and agent.

    python setup_agent.py

The first run creates both and saves their IDs to .bluenile.json. Later runs
update the agent in place, which publishes a new version; existing sessions
keep the version they started with.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import anthropic

from bluenile.agent_config import AGENT_NAME, MODEL, SYSTEM_PROMPT, agent_tools
from bluenile.tools import ShopTools

STATE_FILE = Path(".bluenile.json")


def main() -> None:
    client = anthropic.Anthropic()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    # Tool definitions don't depend on data, so an in-memory DB is enough here.
    tools = agent_tools(ShopTools(sqlite3.connect(":memory:")))

    if "environment_id" not in state:
        env = client.beta.environments.create(
            name="bluenile-shop",
            config={
                "type": "cloud",
                # Package registries only (pip/npm for website and bug-fix work).
                "networking": {"type": "limited", "allow_package_managers": True, "allow_mcp_servers": False},
            },
        )
        state["environment_id"] = env.id
        print(f"Created environment {env.id}")

    if "agent_id" not in state:
        agent = client.beta.agents.create(name=AGENT_NAME, model=MODEL, system=SYSTEM_PROMPT, tools=tools)
        print(f"Created agent {agent.id} (version {agent.version})")
    else:
        current = client.beta.agents.retrieve(state["agent_id"])
        agent = client.beta.agents.update(
            state["agent_id"], version=current.version, name=AGENT_NAME, model=MODEL,
            system=SYSTEM_PROMPT, tools=tools,
        )
        print(f"Agent {agent.id} is at version {agent.version}")

    state["agent_id"] = agent.id
    state["agent_version"] = agent.version
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    print(f"Saved IDs to {STATE_FILE}")


if __name__ == "__main__":
    main()
