import threading
import time

import pytest
from twilio.request_validator import RequestValidator

from bluenile import phone
from bluenile.phone import PhoneConfig, create_app, speakable

TOKEN = "test-token"
BASE = "https://shop.example.com"


class FakeRunner:
    """Stands in for SessionRunner: scripted replies, optional call-control and delay."""

    def __init__(self, call, script):
        self.call, self.script, self.received = call, script, []

    def turn(self, text):
        self.received.append(text)
        step = self.script.pop(0) if self.script else {"say": "Okay."}
        time.sleep(step.get("delay", 0))
        if step.get("fail"):
            return False
        self.call.on_text(step.get("say", ""))
        if "control" in step:
            self.call.call_control(step.get("transfer", ""))(step["control"], {"reason": "x"})
        return True


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(phone, "WAIT_SLICE", 0.3)

    def build(script, transfer=""):
        runners = {}

        def factory(call):
            r = runners[call.sid] = FakeRunner(call, script)
            return r

        cfg = PhoneConfig(auth_token=TOKEN, public_base_url=BASE, transfer_number=transfer,
                          shop_name="Test Garage")
        app = create_app(factory, cfg)
        client = app.test_client()
        validator = RequestValidator(TOKEN)

        def post(path, **form):
            form.setdefault("CallSid", "CA1")
            sig = validator.compute_signature(BASE + path, form)
            return client.post(path, data=form, headers={"X-Twilio-Signature": sig})

        return post, runners, app
    return build


def test_rejects_unsigned_requests(make):
    post, _, app = make([])
    resp = app.test_client().post("/voice/incoming", data={"CallSid": "CA1"})
    assert resp.status_code == 403


def test_full_call_greet_reply_and_hang_up(make):
    post, runners, _ = make([
        {"say": "Sure, **Monday at nine** works."},
        {"say": "Bye now!", "control": "end_call"},
    ])
    r = post("/voice/incoming", From="+15550001111")
    body = r.get_data(as_text=True)
    assert "Thanks for calling Test Garage" in body and "<Gather" in body and 'input="speech"' in body

    body = post("/voice/speech", SpeechResult="Can I book an oil change?").get_data(as_text=True)
    assert "Sure, Monday at nine works." in body and "**" not in body and "<Gather" in body
    first = runners["CA1"].received[0]
    assert "CALLER mode" in first and "+15550001111" in first and "Can I book an oil change?" in first

    body = post("/voice/speech", SpeechResult="Thanks, that's all").get_data(as_text=True)
    assert "Bye now!" in body and "<Hangup" in body and "<Gather" not in body
    assert runners["CA1"].received[1] == "Caller: Thanks, that's all"


def test_slow_turn_holds_then_polls(make):
    post, _, _ = make([{"say": "Found your Civic.", "delay": 0.8}])
    post("/voice/incoming", From="+1555")
    body = post("/voice/speech", SpeechResult="Is my car ready?").get_data(as_text=True)
    assert "One moment please." in body and "/voice/wait?n=1" in body
    for n in range(1, 10):
        body = post(f"/voice/wait?n={n}").get_data(as_text=True)
        if "Found your Civic." in body:
            break
    assert "Found your Civic." in body and "<Gather" in body


def test_transfer_dials_shop(make):
    post, _, _ = make([{"say": "Let me connect you.", "control": "transfer_to_staff",
                        "transfer": "+15559990000"}], transfer="+15559990000")
    post("/voice/incoming")
    body = post("/voice/speech", SpeechResult="I want a person").get_data(as_text=True)
    assert "Let me connect you." in body and "<Dial>+15559990000</Dial>" in body


def test_agent_failure_apologizes(make):
    post, _, _ = make([{"fail": True}])
    post("/voice/incoming")
    body = post("/voice/speech", SpeechResult="hello").get_data(as_text=True)
    assert "having trouble" in body and "<Hangup" in body


def test_silence_reprompts_then_hangs_up(make):
    post, _, _ = make([])
    post("/voice/incoming")
    for _ in range(phone.MAX_SILENCES):
        body = post("/voice/speech", SpeechResult="").get_data(as_text=True)
        assert "didn't catch that" in body
    body = post("/voice/speech", SpeechResult="").get_data(as_text=True)
    assert "<Hangup" in body


def test_caller_hangup_tells_agent(make):
    post, runners, app = make([{"say": "Hi!"}, {"say": "logged"}])
    post("/voice/incoming")
    post("/voice/speech", SpeechResult="hi")
    assert post("/voice/status", CallStatus="completed").status_code == 204
    deadline = time.time() + 3
    while time.time() < deadline and len(runners["CA1"].received) < 2:
        time.sleep(0.05)
    assert runners["CA1"].received[-1].startswith("[Call ended]")
    while time.time() < deadline and app.config["CALLS"].get("CA1"):
        time.sleep(0.05)
    assert app.config["CALLS"].get("CA1") is None


def test_speakable():
    assert speakable("- **Oil change**: $49 [book](http://x)\n# Hours") == "Oil change: $49 book Hours"
