"""bin/ask.py: non-blocking asks. Discord REST and the wake route are stubbed."""
import importlib.util
import json
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ask(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAKOS_HOME", str(tmp_path))
    monkeypatch.setenv("OWNER_DISCORD_ID", "1001")
    monkeypatch.delenv("ASK_EXTRA_OWNER_IDS", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "channels.json").write_text(
        json.dumps({"channels": {"main": {"id": "555"}}}))
    spec = importlib.util.spec_from_file_location("ask_mod", ROOT / "bin" / "ask.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.calls, m.wakes, m.reactions = [], [], {}

    def fake_discord(method, path, body=None):
        m.calls.append((method, path, body))
        if method == "POST":
            return 200, {"id": "900"}
        if method == "GET" and "/reactions/" in path:
            idx = next((i for i, e in enumerate(m.NUMBER_EMOJI)
                        if m.urllib.parse.quote(e) in path), -1)
            return 200, [{"id": u} for u in m.reactions.get(idx, [])]
        return 200, {}

    monkeypatch.setattr(m, "discord", fake_discord)
    monkeypatch.setattr(m, "wake", lambda a: (m.wakes.append(a["id"]) or True))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    return m


def spec(**over):
    s = {"channel": "main", "title": "T", "problem": "P",
         "options": [{"label": "A", "rationale": "ra"}, {"label": "B"}],
         "recommendation": 1, "on_answer": {"type": "poke", "agent": "main"}}
    s.update(over)
    return s


def post(ask, tmp_path, **over):
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(spec(**over)))
    return ask.cmd_post(str(f))


@pytest.mark.parametrize("over,needle", [
    ({"options": [{"label": "A"}]}, "options"),
    ({"options": [{"label": str(i)} for i in range(10)]}, "options"),
    ({"recommendation": 5}, "index"),
    ({"recommendation": None}, "no_recommendation"),
    ({"on_answer": None}, "on_answer"),
])
def test_validate_refuses_each_case_distinctly(ask, over, needle):
    with pytest.raises(ask.AskError, match=needle):
        ask.validate(spec(**over))


def test_null_recommendation_with_reason_ok(ask):
    ask.validate(spec(recommendation=None, no_recommendation="coin flip"))


def test_embed_marks_recommended_and_recolours(ask, tmp_path):
    aid = post(ask, tmp_path)
    a = ask.load(aid)
    e = ask.build_embed(a)
    assert e["color"] == ask.BLUE
    assert any("(recommended)" in f["name"] and f["name"].startswith("2.") for f in e["fields"])
    assert ask.cmd_answer(aid, 0, "1001") == "answered"
    assert ask.build_embed(ask.load(aid))["color"] == ask.GREEN
    aid2 = post(ask, tmp_path)
    ask.close(aid2, "cancelled", "nah")
    assert ask.build_embed(ask.load(aid2))["color"] == ask.GREY


def test_owner_answer_fires_once_second_refused_nonowner_ignored(ask, tmp_path):
    aid = post(ask, tmp_path)
    assert ask.cmd_answer(aid, 0, "2002") == "ignored"
    assert ask.load(aid)["status"] == "open"
    assert ask.cmd_answer(aid, 1, "1001") == "answered"
    assert ask.cmd_answer(aid, 0, "1001") == "already"
    assert ask.wakes == [aid]
    assert ask.load(aid)["answer"]["label"] == "B" and ask.load(aid)["wake"] == "sent"


def test_concurrent_answers_first_wins(ask, tmp_path):
    aid = post(ask, tmp_path)
    out = []
    ts = [threading.Thread(target=lambda i=i: out.append(ask.cmd_answer(aid, i % 2, "1001")))
          for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("answered") == 1 and len(ask.wakes) == 1


def test_failed_wake_is_retried_by_poll(ask, tmp_path, monkeypatch):
    aid = post(ask, tmp_path)
    monkeypatch.setattr(ask, "wake", lambda a: False)
    ask.cmd_answer(aid, 0, "1001")
    assert ask.load(aid)["wake"] == "wake_failed"
    monkeypatch.setattr(ask, "wake", lambda a: (ask.wakes.append(a["id"]) or True))
    ask.cmd_poll()
    assert ask.load(aid)["wake"] == "sent" and ask.wakes == [aid]


def test_reaction_poll_first_owner_wins_nonowner_ignored(ask, tmp_path):
    aid = post(ask, tmp_path)
    ask.reactions = {0: ["2002"], 1: ["1001"]}
    ask.cmd_poll()
    a = ask.load(aid)
    assert a["status"] == "answered" and a["answer"]["index"] == 1


def test_cancel_resolve_expiry(ask, tmp_path):
    a1, a2, a3 = (post(ask, tmp_path) for _ in range(3))
    ask.close(a1, "cancelled", "x")
    ask.close(a2, "resolved", "settled elsewhere")
    d = ask.load(a3)
    d["expires"] = 1
    ask.save(d)                      # expired while the process was down
    ask.cmd_poll()
    assert [ask.load(i)["status"] for i in (a1, a2, a3)] == ["cancelled", "resolved", "expired"]
    with pytest.raises(ask.AskError):
        ask.close(a1, "cancelled", "again")


def test_custom_id_round_trip_and_blocking_ids_untouched(ask, tmp_path):
    aid = post(ask, tmp_path)
    cid = ask.components(ask.load(aid))[0]["components"][1]["custom_id"]
    assert cid == f"kask2:{aid}:1"
    import sys
    sys.path.insert(0, str(ROOT / "bin"))
    import ask_handler
    assert ask_handler.parse_custom_id(cid) is None
    assert ask_handler.parse_custom_id(ask_handler.CUSTOM_ID_PREFIX + ":abc:1") is not None


def test_ask_poll_job_registered():
    import sys
    sys.path.insert(0, str(ROOT / "lib"))
    import job_registry
    assert "ask-poll" in {j.name for j in job_registry.BUILTIN}
