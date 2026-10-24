"""The one reader of a channel's `reply_gate` setting.

`"reply_gate": true` is the heuristic gate only. The key may also be an object
that opts the channel into the classifier tier:

    {"classifier": "haiku", "context_messages": 6, "min_confidence": 0.7,
     "timeout_s": 8, "max_per_minute": 4, "max_per_hour": 60}

An object without `classifier` is heuristic only. Bad values warn once and fall
back to the default; nothing here raises.

Default: with no `reply_gate` key, a channel served by two or more agents (or
with `guest_agents`) is gated (heuristic only). `"reply_gate": false` opts a
channel out; `KARAKOS_REPLY_GATE_DEFAULT=off` restores opt-in everywhere.
Single-agent channels are never gated by default.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("reply_gate_config")

KILL_SWITCH_ENV = "KARAKOS_REPLY_CLASSIFIER"
DEFAULT_ENV = "KARAKOS_REPLY_GATE_DEFAULT"
CLASSIFIERS = ("haiku",)

# key -> (default, low, high, integer?)
_BOUNDS = {
    "context_messages": (6, 0, 20, True),
    "min_confidence": (0.7, 0.5, 1.0, False),
    "timeout_s": (8.0, 2.0, 30.0, False),
    "max_per_minute": (4, 1, 20, True),
    "max_per_hour": (60, 1, 600, True),
}

_warned = set()


def _warn_once(key: str, msg: str, *args):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


@dataclass(frozen=True)
class GateConfig:
    enabled: bool = False
    classifier: Optional[str] = None
    context_messages: int = 6
    min_confidence: float = 0.7
    timeout_s: float = 8.0
    max_per_minute: int = 4
    max_per_hour: int = 60


def _bounded(name: str, raw):
    default, low, high, integer = _BOUNDS[name]
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _warn_once(f"{name}:type", "reply_gate.%s must be a number; using %s", name, default)
        return default
    if not (low <= raw <= high):
        _warn_once(f"{name}:range", "reply_gate.%s=%s is outside %s..%s; using %s",
                   name, raw, low, high, default)
        return default
    return int(raw) if integer else float(raw)


def parse(value) -> GateConfig:
    """Interpret a channel's `reply_gate` value."""
    if not value:
        return GateConfig()
    if not isinstance(value, dict):
        return GateConfig(enabled=True)  # `true`: heuristic only, as ever

    classifier = value.get("classifier")
    if classifier is not None and classifier not in CLASSIFIERS:
        _warn_once(f"classifier:{classifier!r}",
                   "reply_gate.classifier=%r is not supported (only %s); "
                   "using the heuristic gate only", classifier, ", ".join(CLASSIFIERS))
        classifier = None
    if classifier and os.environ.get(KILL_SWITCH_ENV, "").strip().lower() == "off":
        classifier = None

    return GateConfig(
        enabled=True,
        classifier=classifier,
        **{name: _bounded(name, value.get(name)) for name in _BOUNDS},
    )


def effective(value, agent_count: int) -> GateConfig:
    """The gate for a channel: `value` is its `reply_gate` key (None = absent),
    `agent_count` the number of agents in the channel. An absent key turns the
    gate on for channels with two or more agents, unless the env switch says
    off. An explicit value (including `false`) is parsed as written."""
    if value is None:
        if agent_count >= 2 and os.environ.get(DEFAULT_ENV, "").strip().lower() != "off":
            return GateConfig(enabled=True)
        return GateConfig()
    return parse(value)
