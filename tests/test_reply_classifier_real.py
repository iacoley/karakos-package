"""Real `claude` + haiku against three canned cases. Slow; run in the daily
release gate, not the unit job. Skips locally without a credential."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import reply_classifier as rc  # noqa: E402
import reply_gate_config as gc  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.realcli]

HAVE_CRED = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                 or (Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
                     / ".credentials.json").exists())


def _need_cli():
    if shutil.which("claude") is None or not HAVE_CRED:
        if os.environ.get("CI") or os.environ.get("RELEASE_GATE"):
            pytest.fail("real claude CLI/credential required in the release gate")
        pytest.skip("no claude CLI or credential")


def _ask(context, message):
    cfg = gc.parse({"classifier": "haiku", "timeout_s": 30})
    return asyncio.run(rc.classify(cfg, ["amos"], context, message, runner=rc.run_claude))


def _check(v):
    # Budget cap must keep at least 2x headroom over the measured per-call cost
    # (measured 2026-10-03: ~$0.015-0.017 against the $0.05 cap).
    assert v.cost < float(rc.BUDGET_CAP_USD) / 2, v
    assert v.reason not in ("error", "exit", "empty", "timeout", "unparseable", "budget"), v
    return v


def test_direct_question_by_name_engages():
    _need_cli()
    v = _check(_ask([("alex", "morning")], ("mike", "amos, what's on the calendar today?")))
    assert v.engage, v


def test_people_arranging_lunch_stay_silent():
    _need_cli()
    v = _check(_ask([("alex", "are you free for lunch?")],
                    ("mike", "yeah, noon works. should I pick you up?")))
    assert not v.engage, v


def test_ambiguous_room_question_parses():
    _need_cli()
    _check(_ask([("alex", "ugh, it smells like smoke")],
                ("mike", "does anyone know if the oven is on?")))
