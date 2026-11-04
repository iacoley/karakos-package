"""A button press on a non-blocking ask always gets an answer from the relay.

Discord shows "application didn't respond" when an interaction is never
acknowledged; a press from a non-owner used to end that way.
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("discord", reason="relay.py imports discord.py")

RELAY_PATH = Path(__file__).parent.parent / "bin" / "relay.py"


@pytest.fixture
def relay(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    (ws / "bin").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("relay_ask_press_under_test", RELAY_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["relay_ask_press_under_test"] = mod
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    mod._ws = ws
    return mod


def _press(relay, outcome, exit_code=0):
    (relay._ws / "bin" / "ask.py").write_text(
        f"import sys\nprint({outcome!r})\nsys.exit({exit_code})\n")
    cls = next(v for v in vars(relay).values()
               if isinstance(v, type) and hasattr(v, "_route_ask2"))
    calls = []

    class Resp:
        async def defer(self):
            calls.append(("defer", None))

        async def send_message(self, text, ephemeral=False):
            calls.append(("send", ephemeral))

    me = SimpleNamespace()
    me._ask2_note = lambda i, n: cls._ask2_note(me, i, n)
    interaction = SimpleNamespace(user=SimpleNamespace(id=5), response=Resp())
    assert asyncio.run(cls._route_ask2(me, interaction, "kask2:ask-x:0")) is True
    return calls


def test_owner_press_is_deferred(relay):
    assert _press(relay, "answered") == [("defer", None)]


def test_non_owner_press_gets_an_ephemeral_note(relay):
    assert _press(relay, "ignored") == [("send", True)]


def test_unrelated_custom_id_is_not_ours(relay):
    cls = next(v for v in vars(relay).values()
               if isinstance(v, type) and hasattr(v, "_route_ask2"))
    assert asyncio.run(cls._route_ask2(SimpleNamespace(), SimpleNamespace(), "other:1:2")) is False
