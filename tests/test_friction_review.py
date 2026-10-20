"""Friction review (#237 build B): grouping, drafts, report, gated filing."""
import json
import sys
from datetime import timedelta, datetime, timezone

import pytest

from conftest import PACKAGE_ROOT, import_script
from lib import friction_review as fr, job_registry


def ev(kind="tool_error", tool="Read", detail="File does not exist: /a/b/c", agent="marvin", ago=0):
    t = datetime.now(timezone.utc) - timedelta(days=ago)
    return {"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent, "kind": kind, "tool": tool, "detail": detail, "stream": "s"}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "data").mkdir()
    return tmp_path


def put(ws, rows):
    (ws / "data" / "friction-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_groups_by_normalised_cause_and_applies_threshold():
    rows = [ev(detail=f"File does not exist: /x/{i}/file{i}.txt") for i in range(4)]
    rows += [ev(kind="bash_exit", tool="Bash", detail="Exit code 2 | ls") for _ in range(2)]
    got = fr.group(rows)
    assert len(got) == 1 and got[0]["count"] == 4 and got[0]["kind"] == "tool_error"
    assert len(fr.group(rows, min_count=2)) == 2


def test_causes_sorted_by_count_with_stable_ids():
    rows = [ev(tool="A")] * 3 + [ev(tool="B")] * 5
    got = fr.group(rows)
    assert [c["tool"] for c in got] == ["B", "A"]
    assert got[0]["id"] == fr.cause_id("tool_error", "B", got[0]["pattern"])


def test_draft_has_title_fix_and_cause():
    c = fr.group([ev()] * 3)[0]
    d = fr.draft_issue(c)
    assert d["title"].startswith("Friction: tool_error on Read (3x)")
    assert "Proposed fix:" in d["body"] and c["id"] in d["body"]


def test_review_writes_a_dated_report(ws):
    put(ws, [ev()] * 3)
    r = fr.review(ws, today="2026-10-20")
    text = (ws / "data" / "friction" / "2026-10-20.md").read_text()
    assert r["events"] == 3 and len(r["causes"]) == 1
    assert "3 events, 1 recurring cause" in text and "Proposed fix:" in text


def test_empty_log_still_reports(ws):
    fr.review(ws, today="2026-10-20")
    assert "Nothing recurs" in (ws / "data" / "friction" / "2026-10-20.md").read_text()


def two_days(**kw):
    return [ev(**kw), ev(**kw), ev(ago=1, **kw)]


def test_filing_requires_approval_and_files_each_cause_once(ws):
    causes = fr.group(two_days() + two_days(tool="Grep"))
    sent = []
    post = lambda repo, t, b, tok: (sent.append((repo, t)), f"https://x/{len(sent)}")[1]  # noqa: E731
    with pytest.raises(PermissionError):
        fr.file_issues(ws, causes, "o/r", approved=False, post=post)
    with pytest.raises(PermissionError):
        fr.file_issues(ws, causes, "o/r", approved="yes", post=post)
    assert sent == []
    assert len(fr.file_issues(ws, causes, "o/r", True, post=post)) == 2
    assert fr.file_issues(ws, causes, "o/r", True, post=post) == []
    assert len(sent) == 2 and all(r == "o/r" for r, _ in sent)


def test_a_failed_post_keeps_earlier_filings_recorded(ws):
    causes = fr.group(two_days() + two_days(tool="Grep"))
    calls = []
    def post(repo, t, b, tok):
        calls.append(t)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return "https://x/1"
    with pytest.raises(RuntimeError):
        fr.file_issues(ws, causes, "o/r", True, post=post)
    assert len(json.loads((ws / "data" / fr.FILED_REL).read_text())) == 1


def test_cli_never_files_without_yes(ws, capsys):
    put(ws, [ev()] * 3)
    cli = import_script("friction-review")
    assert cli.main(["--workspace", str(ws)]) == 0
    assert "1 recurring cause" in capsys.readouterr().out
    assert cli.main(["--workspace", str(ws), "--file", "--repo", "o/r"]) == 2
    assert not (ws / "data" / fr.FILED_REL).exists()
    with pytest.raises(SystemExit):
        cli.main(["--workspace", str(ws), "--file"])


def test_registered_as_a_nightly_job():
    j = {x.name: x for x in job_registry.BUILTIN}["friction-review"]
    assert j.run == "monitor_jobs.friction_review:run_scheduled"
    sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
    try:
        assert job_registry.resolve_callable(j.run)
    finally:
        sys.path.remove(str(PACKAGE_ROOT / "lib"))


def test_a_cause_seen_on_one_day_is_drafted_but_not_filed(ws):
    causes = fr.group([ev()] * 5)
    assert causes[0]["days"] == 1
    sent = []
    assert fr.file_issues(ws, causes, "o/r", True, post=lambda *a: sent.append(a)) == []
    assert sent == []


# -- the friction MCP action (#237) -------------------------------------------

def _seed(ws, n_days=2):
    put(ws, [ev(ago=i % n_days) for i in range(4)])
    return fr.group(fr.read_log(ws, 7))[0]["id"]


def answer(text):
    return lambda args: {"status": "answered", "answer": text}


def test_recent_returns_rows_and_causes(ws):
    _seed(ws)
    r = fr.recent(ws, 7, limit=2)
    assert r["events"] == 4 and len(r["recent"]) == 2 and r["causes"][0]["count"] == 4


def test_propose_files_only_on_an_explicit_yes(ws):
    cid = _seed(ws)
    sent = []
    post = lambda repo, t, b, tok: (sent.append(t), "https://x/1")[1]  # noqa: E731
    no = fr.propose_issue(ws, cid, "o/r", answer(fr.OPTION_NO), post=post)
    assert no["status"] == "not_filed" and sent == []
    timeout = fr.propose_issue(ws, cid, "o/r", lambda a: {"status": "timeout"}, post=post)
    assert timeout["status"] == "not_filed" and sent == []
    yes = fr.propose_issue(ws, cid, "o/r", answer(fr.OPTION_FILE), post=post)
    assert yes["status"] == "filed" and yes["url"] == "https://x/1" and len(sent) == 1
    again = fr.propose_issue(ws, cid, "o/r", answer(fr.OPTION_FILE), post=post)
    assert again["status"] == "already_filed" and len(sent) == 1


def test_propose_refuses_one_day_causes_bad_repo_and_unknown_ids(ws):
    cid = _seed(ws, n_days=1)
    asked = []
    ask = lambda a: asked.append(a) or {"status": "answered", "answer": fr.OPTION_FILE}  # noqa: E731
    assert fr.propose_issue(ws, cid, "o/r", ask)["status"] == "too_early"
    assert asked == []
    with pytest.raises(ValueError):
        fr.propose_issue(ws, cid, "not a repo", ask)
    with pytest.raises(LookupError):
        fr.propose_issue(ws, "deadbeef00", "o/r", ask)


def test_friction_tool_is_registered_and_dispatches(ws, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    ts = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")
    assert any(t["name"] == "friction" for t in ts.CORE_TOOLS)
    _seed(ws)
    assert ts.handle_core_tool("friction", {"action": "recent"})["events"] == 4
    assert "error" in ts.handle_core_tool("friction", {"action": "propose_issue", "cause": "x", "repo": "bad"})
