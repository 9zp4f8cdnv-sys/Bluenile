"""Which standard service price applies to a vehicle, from its make and model.

The rules live in bluenile/vehicle_classes.json so the shop can edit them.
The website (in the browser), the booking API and the agent all apply the
same rules: the website gets them embedded in the page, and its JavaScript
mirrors `classify()` below.

Rego lookup: getting make and model from a number plate needs a paid
registration-data service. `RegoLookup` is the hook for one; nothing is
configured by default, so customers type their make and model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

CLASSES_FILE = Path(__file__).with_name("vehicle_classes.json")

# rego, state -> {"make", "model", "year"?} or None. Plug a provider in here.
RegoLookup = Callable[[str, str], Optional[dict]]


def load_rules(path: Path = CLASSES_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    """Lower-case, letters and digits only: 'Land Cruiser 200' -> 'landcruiser200'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _make(make: str, rules: dict) -> str:
    """Canonical make key: aliases applied, compared without spaces or punctuation."""
    m = _norm(make)
    aliases = {_norm(k): _norm(v) for k, v in rules.get("make_aliases", {}).items()}
    return aliases.get(m, m)


def classify(make: str, model: str, rules: dict | None = None) -> dict:
    """Returns {"tier": <price list service name>, "reason": <short text>, "matched": bool}."""
    rules = rules or load_rules()
    mk = _make(make, rules)
    md = _norm(model)
    for rule in rules["rules"]:
        models = {(_make(k, rules)): v for k, v in rule.get("models", {}).items()}
        for prefix in models.get(mk, []):
            if _norm(prefix) == "" or md.startswith(_norm(prefix)):
                return {"tier": rule["tier"], "matched": True,
                        "reason": f"{make.strip()} {model.strip()}".strip() + f" is classed as {rule['label']}"}
        if mk in {_make(x, rules) for x in rule.get("makes", [])}:
            return {"tier": rule["tier"], "matched": True,
                    "reason": f"{make.strip()} vehicles are classed as {rule['label']}"}
    return {"tier": rules["default"], "matched": False,
            "reason": f"{make.strip()} {model.strip()}".strip() + " gets the standard price"}
