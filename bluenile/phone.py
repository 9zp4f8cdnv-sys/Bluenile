"""Real phone line: a Twilio Programmable Voice webhook server.

How a call flows:
1. Twilio POSTs /voice/incoming. We greet the caller right away and start a
   Managed Agents session for the call in the background.
2. Twilio transcribes what the caller says (<Gather input="speech">) and POSTs
   it to /voice/speech. We send it to the agent as one turn.
3. Agent turns can take several seconds and Twilio waits at most 15 s for a
   webhook, so we wait up to WAIT_SLICE seconds; if the reply isn't ready we
   say "one moment" and have Twilio poll /voice/wait until it is.
4. The reply is read aloud with <Say>, inside a new <Gather> for the next
   thing the caller says. If the agent called end_call we hang up; if it
   called transfer_to_staff we <Dial> the shop's transfer number.
5. Twilio POSTs /voice/status when the call ends. If the caller hung up first,
   we tell the agent so it can log the call.

Every request is checked against Twilio's X-Twilio-Signature.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from flask import Flask, Response, abort, request
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

from .session import MODE_NOTES, SessionRunner

log = logging.getLogger("bluenile.phone")

WAIT_SLICE = 4.0          # seconds to hold one webhook open waiting for the agent
MAX_WAIT = 60.0           # give up on a turn after this long
SESSION_START_TIMEOUT = 30.0
MAX_SILENCES = 2          # hang up after this many empty listens in a row
ENDED_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


@dataclass
class PhoneConfig:
    auth_token: str = field(default_factory=lambda: os.environ.get("TWILIO_AUTH_TOKEN", ""))
    public_base_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"))
    transfer_number: str = field(default_factory=lambda: os.environ.get("SHOP_TRANSFER_NUMBER", ""))
    voice: str = field(default_factory=lambda: os.environ.get("TWILIO_VOICE", "Polly.Joanna-Neural"))
    language: str = field(default_factory=lambda: os.environ.get("TWILIO_LANGUAGE", "en-US"))
    shop_name: str = field(default_factory=lambda: os.environ.get("SHOP_NAME", "Bluenile Auto Repair"))

    @property
    def greeting(self) -> str:
        return f"Thanks for calling {self.shop_name}. How can I help you today?"


# ---------------------------------------------------------------------------
# Per-call state
# ---------------------------------------------------------------------------

class Call:
    def __init__(self, sid: str, from_number: str):
        self.sid = sid
        self.from_number = from_number
        self.runner: SessionRunner | None = None
        self.session_ready = threading.Event()
        self.session_error: str | None = None
        self.turn_lock = threading.Lock()   # one agent turn at a time
        self.turn_done = threading.Event()
        self.turn_done.set()
        self.turn_failed = False
        self.turn_started_at = 0.0
        self.reply_parts: list[str] = []
        self.first_turn = True
        self.action: str | None = None      # None | "hangup" | "transfer"
        self.silences = 0
        self.ended = False

    # Called by SessionRunner from the turn thread.
    def on_text(self, text: str) -> None:
        self.reply_parts.append(text)

    def call_control(self, transfer_number: str) -> Callable[[str, dict], str]:
        def control(name: str, args: dict) -> str:
            if name == "end_call":
                self.action = "hangup"
                return "The call will end after your last message is spoken."
            if name == "transfer_to_staff":
                if not transfer_number:
                    return ("No transfer line is set up. Tell the caller someone will call them "
                            "back, and log_call with a follow_up.")
                self.action = "transfer"
                return "The caller will be transferred after your last message is spoken."
            return f"Unknown call control {name}"
        return control

    def take_reply(self) -> str:
        text = " ".join(p.strip() for p in self.reply_parts if p.strip())
        self.reply_parts.clear()
        return text


RunnerFactory = Callable[[Call], SessionRunner]


class CallManager:
    def __init__(self, runner_factory: RunnerFactory):
        self.runner_factory = runner_factory
        self.calls: dict[str, Call] = {}
        self.lock = threading.Lock()

    def get(self, sid: str) -> Call | None:
        with self.lock:
            return self.calls.get(sid)

    def start(self, sid: str, from_number: str) -> Call:
        with self.lock:
            if sid in self.calls:  # Twilio retried the webhook
                return self.calls[sid]
            call = self.calls[sid] = Call(sid, from_number)

        def create():
            try:
                call.runner = self.runner_factory(call)
            except Exception as e:  # noqa: BLE001 - reported to the caller as a transfer/apology
                log.exception("could not start session for %s", sid)
                call.session_error = repr(e)
            finally:
                call.session_ready.set()

        threading.Thread(target=create, daemon=True, name=f"session-{sid}").start()
        return call

    def send_turn(self, call: Call, text: str) -> None:
        """Start an agent turn in the background; poll call.turn_done for the result."""
        call.turn_done.clear()
        call.turn_failed = False
        call.turn_started_at = time.monotonic()

        def work():
            with call.turn_lock:
                try:
                    if not call.session_ready.wait(SESSION_START_TIMEOUT) or call.runner is None:
                        raise RuntimeError(call.session_error or "session did not start")
                    if not call.runner.turn(text):
                        raise RuntimeError("lost connection to the agent")
                except Exception:  # noqa: BLE001
                    log.exception("turn failed for %s", call.sid)
                    call.turn_failed = True
                finally:
                    call.turn_done.set()

        threading.Thread(target=work, daemon=True, name=f"turn-{call.sid}").start()

    def finish(self, sid: str) -> None:
        """The call is over. If the caller hung up on the agent, let it log the call."""
        call = self.get(sid)
        if call is None or call.ended:
            return
        call.ended = True

        def work():
            call.turn_done.wait(MAX_WAIT)  # let a turn in flight finish first
            # The agent ended or transferred the call itself, or the caller never spoke:
            # nothing to tell it.
            if call.action is None and call.runner is not None and not call.first_turn:
                self.send_turn(call, "[Call ended] The caller hung up. Log the call now if you haven't.")
                call.turn_done.wait(MAX_WAIT)
            with self.lock:
                self.calls.pop(sid, None)

        threading.Thread(target=work, daemon=True, name=f"finish-{sid}").start()


# ---------------------------------------------------------------------------
# Speech formatting
# ---------------------------------------------------------------------------

_LINE_MARKS = re.compile(r"^\s*(?:[-•]|#+|>)\s+", re.MULTILINE)   # bullets, headings, quotes
_INLINE_MARKS = re.compile(r"[*_`|]+")                              # emphasis, code, tables


def speakable(text: str) -> str:
    """Strip markdown and symbols that text-to-speech would read out literally."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
    text = _LINE_MARKS.sub(" ", text)
    text = _INLINE_MARKS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app(runner_factory: RunnerFactory, config: PhoneConfig | None = None) -> Flask:
    cfg = config or PhoneConfig()
    if not cfg.auth_token:
        raise RuntimeError("TWILIO_AUTH_TOKEN is required to verify that requests come from Twilio")
    validator = RequestValidator(cfg.auth_token)
    manager = CallManager(runner_factory)
    app = Flask(__name__)
    app.config["CALLS"] = manager

    def public_url() -> str:
        # Twilio signs the public URL it called, which differs from request.url
        # behind ngrok or a load balancer.
        if cfg.public_base_url:
            return cfg.public_base_url + request.full_path.rstrip("?")
        return request.url

    def twilio_only(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            sig = request.headers.get("X-Twilio-Signature", "")
            if not validator.validate(public_url(), request.form.to_dict(), sig):
                log.warning("rejected request with bad Twilio signature: %s", request.path)
                abort(403)
            return fn(*a, **kw)
        return wrapper

    def twiml(r: VoiceResponse) -> Response:
        return Response(str(r), mimetype="text/xml")

    def listen(r: VoiceResponse, say: str | None) -> VoiceResponse:
        g = Gather(input="speech", action="/voice/speech", method="POST", speech_timeout="auto",
                   language=cfg.language, action_on_empty_result=True)
        if say:
            g.say(say, voice=cfg.voice)
        r.append(g)
        return r

    def reply(call: Call) -> Response:
        """Turn the finished agent turn into TwiML."""
        r = VoiceResponse()
        text = speakable(call.take_reply())
        if call.turn_failed:
            text = "Sorry, I'm having trouble on my end."
            if cfg.transfer_number:
                r.say(text + " Let me get someone from the shop.", voice=cfg.voice)
                r.dial(cfg.transfer_number)
            else:
                r.say(text + " Please call back in a few minutes. Goodbye.", voice=cfg.voice)
                r.hangup()
            return twiml(r)
        if call.action == "transfer":
            if text:
                r.say(text, voice=cfg.voice)
            r.dial(cfg.transfer_number)
            return twiml(r)
        if call.action == "hangup":
            if text:
                r.say(text, voice=cfg.voice)
            r.hangup()
            return twiml(r)
        return twiml(listen(r, text or "Is there anything else I can help with?"))

    def wait_or_hold(call: Call, attempt: int) -> Response:
        if call.turn_done.wait(WAIT_SLICE):
            return reply(call)
        if time.monotonic() - call.turn_started_at > MAX_WAIT:
            call.turn_failed = True
            return reply(call)
        r = VoiceResponse()
        if attempt == 0:
            r.say("One moment please.", voice=cfg.voice)
        elif attempt % 4 == 0:
            r.say("Still checking, thanks for waiting.", voice=cfg.voice)
        else:
            r.pause(length=1)
        r.redirect(f"/voice/wait?n={attempt + 1}", method="POST")
        return twiml(r)

    @app.post("/voice/incoming")
    @twilio_only
    def incoming():
        sid = request.form.get("CallSid", "")
        if not sid:
            abort(400)
        manager.start(sid, request.form.get("From", ""))
        return twiml(listen(VoiceResponse(), cfg.greeting))

    @app.post("/voice/speech")
    @twilio_only
    def speech():
        call = manager.get(request.form.get("CallSid", ""))
        if call is None:  # server restarted mid-call
            r = VoiceResponse()
            r.say("Sorry, we got disconnected. Please call back.", voice=cfg.voice)
            r.hangup()
            return twiml(r)
        said = request.form.get("SpeechResult", "").strip()
        if not said:
            call.silences += 1
            r = VoiceResponse()
            if call.silences > MAX_SILENCES:
                r.say("I didn't hear anything, so I'll hang up. Please call back anytime. Goodbye.",
                      voice=cfg.voice)
                r.hangup()
                return twiml(r)
            return twiml(listen(r, "Sorry, I didn't catch that. How can I help?"))
        call.silences = 0
        if not call.turn_done.is_set():  # shouldn't happen: we don't listen mid-turn
            return wait_or_hold(call, 1)
        if call.first_turn:
            call.first_turn = False
            caller_id = call.from_number or "unknown"
            said = (f"{MODE_NOTES['caller']}\n[Caller ID: {caller_id}] "
                    f"You already greeted them with: \"{cfg.greeting}\"\n\nCaller: {said}")
        else:
            said = f"Caller: {said}"
        manager.send_turn(call, said)
        return wait_or_hold(call, 0)

    @app.post("/voice/wait")
    @twilio_only
    def wait():
        call = manager.get(request.form.get("CallSid", ""))
        if call is None:
            r = VoiceResponse()
            r.hangup()
            return twiml(r)
        return wait_or_hold(call, int(request.args.get("n", "1")))

    @app.post("/voice/status")
    @twilio_only
    def status():
        if request.form.get("CallStatus") in ENDED_STATUSES:
            manager.finish(request.form.get("CallSid", ""))
        return ("", 204)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "active_calls": len(manager.calls)}

    return app
