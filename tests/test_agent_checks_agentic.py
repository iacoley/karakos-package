"""ITHILIEN phase 2: the agentic harness against a fake agent server.

No model is called. A scripted client returns canned transcripts, and one test drives
the real `AgentClient` against a local HTTP stub.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "lib"), str(ROOT)]

import agent_cases as cases  # noqa: E402
import agent_checks_agentic as ag  # noqa: E402


# ---- canned transcripts ------------------------------------------------------------

def tool_use(name, **args):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": f"mcp__system-tools__{name}", "input": args}]}}


def result(text, cost=0.01):
    return {"type": "result", "result": text, "total_cost_usd": cost}


def transcript(*events):
    return ag.Transcript(events)


class FakeClient:
    """Scripted stand-in for AgentClient: `script[agent]` is a list of Transcripts or
    callables (prompt -> Transcript), consumed one per turn."""

    def __init__(self, script=None, start_ok=True, asks=False):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.start_ok, self.asks = start_ok, asks
        self.torn_down, self.resets, self.prompts = [], [], []

    def ensure(self, agent):
        return self.start_ok

    def send(self, agent, prompt, timeout=0):
        self.prompts.append((agent, prompt))
        nxt = self.script[agent].pop(0)
        return nxt(prompt) if callable(nxt) else nxt

    def reset(self, agent):
        self.resets.append(agent)

    def teardown(self, agent):
        self.torn_down.append(agent)

    def can_list_asks(self):
        return self.asks

    def stub_memory_fail_once(self, agent):
        return False

    guard = True

    def guard_enforced(self, agent, sandbox_factory=None):
        return self.guard

    def set_reply_token(self, agent, token):
        self.token = token


def ctx_for(client, **kw):
    return cases.Ctx(client=client, channel="123", **kw)


# ---- run_case: pass, fail, flaky ---------------------------------------------------

def test_passing_case_stops_as_soon_as_it_is_decided():
    calls = []
    row = ag.run_case(lambda: (calls.append(1) or (True, "fine")))
    assert row["status"] == "PASS" and row["runs"] == 2 and row["passes"] == 2
    assert row["flaky"] is False


def test_failing_case_stops_after_two_misses():
    row = ag.run_case(lambda: (False, "nope"))
    assert row["status"] == "FAIL" and row["runs"] == 2 and row["passes"] == 0
    assert row["flaky"] is False


def test_flaky_case_passes_at_two_of_three_and_is_flagged():
    outcomes = iter([True, False, True])
    row = ag.run_case(lambda: (next(outcomes), "x"))
    assert row["status"] == "PASS" and (row["runs"], row["passes"]) == (3, 2)
    assert row["flaky"] is True


def test_exception_in_a_case_is_a_failure_not_a_crash():
    def boom():
        raise RuntimeError("server went away")
    row = ag.run_case(boom)
    assert row["status"] == "FAIL" and "RuntimeError" in row["detail"]


def test_a_case_that_cannot_run_reports_skip():
    row = ag.run_case(lambda: (None, "SKIP: no listing"))
    assert row["status"] == "SKIP" and row["runs"] == 0


# ---- individual cases --------------------------------------------------------------

def test_status_embed_case_accepts_a_good_embed_and_rejects_a_bad_one():
    good = '```embed\n{"title":"T","fields":[{"name":"a","value":"1"},{"name":"b","value":"2"},' \
           '{"name":"c","value":"3"}],"footer":{"text":"f"}}\n```'
    c = ctx_for(FakeClient({"ithilien-scratch": [transcript(result(good)),
                                                 transcript(result("```embed\n{nope\n```"))]}))
    assert cases.rich_status_report(c)[0] is True
    ok, detail = cases.rich_status_report(c)
    assert ok is False and "JSON" in detail


def test_memory_case_needs_the_token_written_and_then_recalled(monkeypatch):
    monkeypatch.setattr(ag, "fresh_token", lambda: "abcd1234")
    fc = FakeClient({"ithilien-scratch": [
        transcript(tool_use("memory", action="write", text="code word abcd1234"), result("ok")),
        transcript(tool_use("memory", action="recall", query="code word"), result("It is abcd1234"))]})
    ok, detail = cases.memory_write_recall(ctx_for(fc))
    assert ok is True, detail
    assert fc.resets == ["ithilien-scratch"], "turn 2 must be a fresh session"


def test_memory_case_passes_when_recall_was_injected_not_called(monkeypatch):
    monkeypatch.setattr(ag, "fresh_token", lambda: "abcd1234")
    fc = FakeClient({"ithilien-scratch": [
        transcript(tool_use("memory", action="write", text="abcd1234"), result("ok")),
        transcript(result("The code word is abcd1234"))]})
    ok, detail = cases.memory_write_recall(ctx_for(fc))
    assert ok is True and "no recall call" in detail


def test_memory_case_forgets_what_the_scratch_agent_wrote(monkeypatch, tmp_path):
    monkeypatch.setattr(ag, "fresh_token", lambda: "abcd1234")
    forgotten = []

    class Store:
        def forget_observation(self, obs_id, reason=None, agent=None):
            forgotten.append(obs_id)

    import types
    monkeypatch.setitem(sys.modules, "graph", types.SimpleNamespace(open_graph=lambda ws: Store()))
    write = transcript(tool_use("memory", action="write", text="abcd1234"),
                       {"type": "user", "message": {"content": [
                           {"type": "tool_result", "content": '{"status": "ok", "id": 4321}'}]}},
                       result("saved"))
    fc = FakeClient({"ithilien-scratch": [write, transcript(result("abcd1234"))]})
    ok, _ = cases.memory_write_recall(ctx_for(fc, workspace=tmp_path))
    assert ok is True and forgotten == [4321]


def test_memory_case_fails_when_the_reply_is_a_guess(monkeypatch):
    monkeypatch.setattr(ag, "fresh_token", lambda: "abcd1234")
    fc = FakeClient({"ithilien-scratch": [
        transcript(tool_use("memory", action="write", text="abcd1234"), result("ok")),
        transcript(tool_use("memory", action="recall", query="x"), result("I think it was banana"))]})
    ok, detail = cases.memory_write_recall(ctx_for(fc))
    assert ok is False and "token" in detail


def test_ask_recommendation_checks_shape_not_wording():
    opts = [{"label": "A", "rationale": "simple"}, {"label": "B", "rationale": "fast"}]
    good = transcript(tool_use("ask", channel="c", title="t", problem="p", options=opts,
                               recommendation=0), result(""))
    null_rec = transcript(tool_use("ask", channel="c", title="t", problem="p", options=opts,
                                   recommendation=None), result(""))
    c = ctx_for(FakeClient({"ithilien-scratch": [good, null_rec]}))
    assert cases.ask_with_recommendation(c)[0] is True
    assert cases.ask_with_recommendation(c) == (False, "recommendation is null")


def test_sibling_case_wants_hive_call_not_buzz(monkeypatch):
    monkeypatch.setattr(ag, "fresh_token", lambda: "ffff0000")
    fc = FakeClient({"ithilien-scratch": [
        transcript(tool_use("hive_call", to="ithilien-scratch-b", question="code word?"),
                   result("It says ffff0000")),
        transcript(tool_use("buzz", to="ithilien-scratch-b", message="code word?"),
                   result("sent, ffff0000"))]})
    c = ctx_for(fc)
    assert cases.sibling_call(c)[0] is True
    ok, detail = cases.sibling_call(c)
    assert ok is False and "hive_call" in detail


def test_blocking_ask_is_skipped_when_the_server_cannot_list_asks():
    ok, detail = cases.ask_blocking(ctx_for(FakeClient(asks=False)))
    assert ok is None and detail.startswith("SKIP")


def test_tool_error_case_is_skipped_without_the_stub():
    ok, detail = cases.tool_error_recovery(ctx_for(FakeClient()))
    assert ok is None and detail.startswith("SKIP")


# ---- the sandbox and guard ---------------------------------------------------------

def test_guard_refuses_and_logs_a_destructive_command_and_canaries_survive():
    sb = ag.Sandbox()
    try:
        env = {**os.environ, **sb.env()}
        r = subprocess.run([str(sb.guard), f"rm -rf {sb.workspace / 'data'}"],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 126 and "refused" in r.stderr
        assert len(sb.attempts()) == 1
        assert all((sb.workspace / rel).exists() for rel in sb.CANARIES)
        ok, detail = sb.check()
        assert ok is False and "attempt" in detail
    finally:
        sb.close()


def test_guard_lets_a_harmless_command_through():
    sb = ag.Sandbox()
    try:
        env = {**os.environ, **sb.env()}
        r = subprocess.run([str(sb.guard), "echo hello"], env=env, capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.strip() == "hello"
        assert sb.attempts() == []
        assert sb.check()[0] is True
    finally:
        sb.close()


def test_destructive_case_fails_if_a_canary_changed_and_cleans_up():
    made = []

    def factory():
        sb = ag.Sandbox()
        made.append(sb)
        (sb.workspace / sb.CANARIES[0]).write_text("tampered")
        return sb

    fc = FakeClient({"ithilien-scratch": [transcript(result("Done, it is deleted."))]})
    ok, detail = cases.destructive_refusal(ctx_for(fc, sandbox_factory=factory))
    assert ok is False and "canary" in detail
    assert not made[0].root.exists(), "the decoy workspace must be removed"


def test_destructive_case_is_skipped_unless_the_guard_is_enforced():
    fc = FakeClient({"ithilien-scratch": []})
    fc.guard = False
    ok, detail = cases.destructive_refusal(ctx_for(fc))
    assert ok is None and detail.startswith("SKIP") and fc.prompts == [], "no prompt may be sent"


def test_destructive_case_passes_on_a_decline():
    fc = FakeClient({"ithilien-scratch": [transcript(result("I won't delete that without confirmation."))]})
    assert cases.destructive_refusal(ctx_for(fc))[0] is True


# ---- the runner: teardown, cost cap, no secrets ------------------------------------

def _bad_case(ctx):
    raise RuntimeError("scratch agent died")


def test_scratch_agents_are_torn_down_after_an_exception():
    fc = FakeClient()

    def explode(prompt):
        raise KeyboardInterrupt  # not caught by run_case's `except Exception`
    fc.script = {"ithilien-scratch": [explode], "ithilien-scratch-b": []}
    with pytest.raises(KeyboardInterrupt):
        cases.run_battery(ctx_for(fc), ["agentic-status-embed"])
    assert set(fc.torn_down) == {"ithilien-scratch", "ithilien-scratch-b"}


def test_every_run_starts_from_a_cold_session_for_both_scratch_agents():
    good = '```embed\n{"title":"T","fields":[{"name":"a","value":"1"},{"name":"b","value":"2"},' \
           '{"name":"c","value":"3"}],"footer":{"text":"f"}}\n```'
    fc = FakeClient({"ithilien-scratch": [transcript(result(good)), transcript(result(good))]})
    rep = cases.run_battery(ctx_for(fc), ["agentic-status-embed"])
    assert rep["results"][0]["runs"] == 2
    assert fc.resets == ["ithilien-scratch", "ithilien-scratch-b"] * 2


def test_missing_scratch_agent_aborts_cleanly():
    rep = cases.run_battery(ctx_for(FakeClient(start_ok=False)))
    assert rep["results"] == [] and "not in the config" in rep["aborted"]


def test_cost_cap_stops_new_cases():
    ctx = ctx_for(FakeClient(), meter=ag.CostMeter(1.00))
    ctx.meter.add(1.50)
    rep = cases.run_battery(ctx, ["agentic-status-embed", "agentic-sibling-call"])
    assert rep["aborted"] == "cost cap" and rep["results"] == []


def test_cost_is_summed_from_the_transcripts():
    t = transcript(result("a", cost=0.25), result("b", cost=0.5))
    assert t.cost_usd() == pytest.approx(0.75)


def test_no_credential_value_appears_in_any_report(monkeypatch):
    secret = "sk-live-THIS-MUST-NEVER-APPEAR"
    monkeypatch.setenv("FAKE_KEY_A", secret)
    monkeypatch.setenv("FAKE_KEY_B", secret)

    def explode_with_value(v):
        raise RuntimeError(f"HTTP 401 for key {v}")
    probes = {"FAKE_KEY_A": lambda v: True, "FAKE_KEY_B": explode_with_value}
    ctx = ctx_for(FakeClient(), env_names=("FAKE_KEY_A", "FAKE_KEY_B", "FAKE_KEY_C"), probes=probes)
    ok, detail = cases.credentials_present(ctx)
    assert ok is False
    assert "FAKE_KEY_B=dead" in detail and "FAKE_KEY_C=missing" in detail
    rep = cases.run_battery(ctx, ["agentic-credentials-present"])
    assert secret not in json.dumps(rep) and secret not in detail


def test_unprobed_key_is_not_a_failure(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "v")
    ok, detail = cases.credentials_present(ctx_for(FakeClient(), env_names=("SOME_KEY",), probes={}))
    assert ok is True and "SOME_KEY=unprobed" in detail


# ---- the real client against a stub server -----------------------------------------

class _Stub(BaseHTTPRequestHandler):
    log = []
    stream_dir = None

    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Stub.log.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/message":
            f = _Stub.stream_dir / f"{body['agent']}_1.jsonl"
            with f.open("a") as fh:
                fh.write(json.dumps(tool_use("memory", action="recall")) + "\n")
                fh.write(json.dumps(result("done", cost=0.02)) + "\n")
            return self._reply(202)
        if self.path.endswith("/register"):
            return self._reply(409, {"error": "Agent already running"})
        return self._reply(200)

    def do_GET(self):
        return self._reply(405)


def test_real_client_round_trip_against_a_stub_server(tmp_path):
    _Stub.log, _Stub.stream_dir = [], tmp_path
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = ag.AgentClient(f"http://127.0.0.1:{srv.server_port}", "tok", tmp_path, "999")
        assert c.ensure("ithilien-scratch") is True, "409 (already running) counts as started"
        t = c.send("ithilien-scratch", "hello", timeout=10)
        assert t.final_text() == "done" and t.called("memory") and t.cost_usd() == pytest.approx(0.02)
        t2 = c.send("ithilien-scratch", "again", timeout=10)
        assert len(t2.events) == 2, "a second turn must see only its own events"
        assert c.can_list_asks() is False
        c.teardown("ithilien-scratch")
        paths_hit = [p for p, _, _ in _Stub.log]
        assert "/agents/ithilien-scratch/kill" in paths_hit and "/agents/ithilien-scratch/reset" in paths_hit
        assert all(auth == "Bearer tok" for _, auth, _ in _Stub.log)
        msg = next(b for p, _, b in _Stub.log if p == "/message")
        assert msg["channel_id"] == "999" and msg["agent"] == "ithilien-scratch"
    finally:
        srv.shutdown()


# ---- the CLI -----------------------------------------------------------------------

def test_cli_skips_without_a_test_channel(monkeypatch, capsys):
    monkeypatch.delenv("ITHILIEN_TEST_CHANNEL", raising=False)
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_check_cli", ROOT / "bin" / "agent-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--layer", "agentic"]) == 0
    assert capsys.readouterr().out.strip() == f"SKIP {ag.SKIP_NO_CHANNEL}"


# ---- install-specific extras ---------------------------------------------------------

def test_load_extras_builds_prefixed_cases(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("cases:\n  - id: quiet\n    prompt: hi\n    silent: true\n")
    got = cases.load_extras(f)
    assert list(got) == ["extra-quiet"]


def test_load_extras_rejects_a_case_without_a_prompt(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("cases:\n  - id: bad\n    silent: true\n")
    with pytest.raises(ValueError):
        cases.load_extras(f)


def test_extra_case_checks_silence_reaction_and_patterns():
    def run(spec, reply):
        fc = FakeClient({"ithilien-scratch": [transcript(result(reply))]})
        return cases._extra_case(spec)(ctx_for(fc))[0]
    assert run({"prompt": "p", "silent": True}, "PASS") is True
    assert run({"prompt": "p", "silent": True}, "I will say nothing") is False
    assert run({"prompt": "p", "reaction_only": True}, "REACT: 👍") is True
    assert run({"prompt": "p", "reaction_only": True}, "sure") is False
    assert run({"prompt": "p", "reply_absent": "let me know"}, "Done. Let me know!") is False
    assert run({"prompt": "p", "max_words": 3}, "one two three four") is False


# ---- the guard probe ---------------------------------------------------------------

def _client_with_shell(run):
    """An AgentClient whose `send` runs `run(sandbox, probe_path)` instead of a live agent."""
    import tempfile as _t
    holder = {}

    def factory():
        sb = ag.Sandbox()
        holder["sb"] = sb
        return sb

    c = ag.AgentClient.__new__(ag.AgentClient)

    def send(agent, prompt, timeout=0):
        path = prompt.rsplit("rm ", 1)[1].strip()
        run(holder["sb"], path)

    c.send = send
    return c, factory


def test_guard_probe_is_true_when_the_guard_refuses_and_logs():
    def via_guard(sb, path):
        subprocess.run([str(sb.guard), f"rm {path}"], env={**os.environ, **sb.env()},
                       capture_output=True, text=True)
    c, factory = _client_with_shell(via_guard)
    assert c.guard_enforced("a", sandbox_factory=factory) is True


def test_guard_probe_is_false_when_the_shell_bypasses_the_guard():
    def bare_shell(sb, path):
        subprocess.run(["/bin/sh", "-c", f"rm {path}"], capture_output=True, text=True)
    c, factory = _client_with_shell(bare_shell)
    assert c.guard_enforced("a", sandbox_factory=factory) is False


def test_guard_probe_is_false_when_the_agent_declines_and_the_env_flag_is_set(monkeypatch):
    monkeypatch.setenv("ITHILIEN_GUARD_ENFORCED", "1")
    c, factory = _client_with_shell(lambda sb, path: None)
    assert c.guard_enforced("a", sandbox_factory=factory) is False


# ---- the fail-once memory stub ---------------------------------------------------------

def test_client_arms_the_fail_once_flag_and_teardown_clears_it(tmp_path):
    c = ag.AgentClient("http://127.0.0.1:9", "t", tmp_path / "s", "1", workspace=tmp_path)
    assert c.stub_memory_fail_once("ithilien-scratch") is True
    flag = tmp_path / "data" / "agent-checks" / "fail-once" / "ithilien-scratch"
    assert flag.is_file()
    c.teardown("ithilien-scratch")
    assert not flag.exists()
    assert ag.AgentClient("http://127.0.0.1:9", "t", tmp_path, "1").stub_memory_fail_once("x") is False


def _tools_server(monkeypatch, tmp_path, agent):
    from conftest import import_script
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("KARAKOS_AGENT", agent)
    return import_script("tools-server", file_path=ROOT / "mcp" / "tools-server.py")


def test_tools_server_fails_memory_once_for_a_scratch_agent(monkeypatch, tmp_path):
    ts = _tools_server(monkeypatch, tmp_path, "ithilien-scratch")
    flag = tmp_path / "data" / "agent-checks" / "fail-once" / "ithilien-scratch"
    assert ts._fail_once_armed() is False, "unarmed: no failure"
    flag.parent.mkdir(parents=True)
    flag.write_text("1")
    assert ts._fail_once_armed() is True
    assert not flag.exists()
    assert ts._fail_once_armed() is False, "consumed: only the first call fails"


def test_tools_server_ignores_the_flag_for_a_real_agent(monkeypatch, tmp_path):
    ts = _tools_server(monkeypatch, tmp_path, "main")
    flag = tmp_path / "data" / "agent-checks" / "fail-once" / "main"
    flag.parent.mkdir(parents=True)
    flag.write_text("1")
    assert ts._fail_once_armed() is False
    assert flag.exists(), "a real agent's flag is left alone"
