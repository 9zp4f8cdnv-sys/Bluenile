from types import SimpleNamespace as NS

import pytest

import run_agent
from bluenile.db import connect
from bluenile.tools import ShopConfig, ShopTools


class FakeEvents:
    def __init__(self):
        self.sent = []

    def send(self, session_id, events):
        self.sent.extend(events)


@pytest.fixture
def runner(tmp_path):
    client = NS(beta=NS(sessions=NS(events=FakeEvents())))
    tools = ShopTools(connect(tmp_path / "db"), ShopConfig(invoice_dir=tmp_path))
    return run_agent.SessionRunner(client, "sesn_1", tools, "staff")


def sent(r):
    return r.client.beta.sessions.events.sent


def test_custom_tool_result_sent_and_dedup(runner):
    ev = NS(id="e1", type="agent.custom_tool_use", name="save_customer", input={"name": "Al"})
    assert runner.handle(ev) is False
    assert runner.handle(ev) is False  # duplicate from history replay is ignored
    assert len(sent(runner)) == 1
    out = sent(runner)[0]
    assert out["type"] == "user.custom_tool_result" and out["custom_tool_use_id"] == "e1"
    assert out["is_error"] is False and '"name": "Al"' in out["content"][0]["text"]


def test_tool_error_returned_as_error(runner):
    runner.handle(NS(id="e2", type="agent.custom_tool_use", name="get_invoice", input={"invoice_id": 9}))
    assert sent(runner)[0]["is_error"] is True


def test_caller_blocked_and_confirm_declined(runner, monkeypatch):
    runner.mode = "caller"
    runner.handle(NS(id="e3", type="agent.custom_tool_use", name="record_payment",
                     input={"invoice_id": 1, "amount": 5, "method": "cash"}))
    assert "staff-only" in sent(runner)[-1]["content"][0]["text"]
    runner.mode = "staff"
    monkeypatch.setattr(run_agent, "ask_yes_no", lambda p: False)
    runner.handle(NS(id="e4", type="agent.custom_tool_use", name="cancel_appointment", input={"appointment_id": 1}))
    assert "declined" in sent(runner)[-1]["content"][0]["text"]


def test_idle_ends_turn_only_when_not_requires_action(runner):
    assert runner.handle(NS(id="i1", type="session.status_idle", stop_reason=NS(type="requires_action"))) is False
    assert runner.handle(NS(id="i2", type="session.status_idle", stop_reason=NS(type="end_turn"))) is True
