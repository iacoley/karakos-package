"""
Tests for bin/kara — CLI client.

Two layers:
  - Subprocess tests exercise the CLI end-to-end (--help, unknown flags,
    missing-token failure) via the self-exec entrypoint.
  - Module-level tests import bin/kara directly (it self-execs under .venv
    when present, so import-time constants are read fresh per test with the
    target env already set) and exercise slash-command dispatch, _http's
    error handling, and _enqueue's payload shape without a running server.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib import error as urlerror

import pytest

from conftest import PACKAGE_ROOT

KARA = PACKAGE_ROOT / "bin" / "kara"


def _run(args, env_extra=None, stdin=None, timeout=10):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(KARA), *args],
        capture_output=True, text=True, env=env, input=stdin, timeout=timeout,
    )


def test_kara_exists_and_executable():
    assert KARA.exists()
    assert os.access(KARA, os.X_OK)


def test_kara_parses():
    """Script must parse as valid Python."""
    import ast
    ast.parse(KARA.read_text())


def test_help_flag():
    proc = _run(["--help"])
    assert proc.returncode == 0
    assert "kara" in proc.stdout.lower()
    assert "AGENT_SERVER_URL" in proc.stdout
    assert "KARA_CHANNEL" in proc.stdout


def test_unknown_flag():
    proc = _run(["--bogus"])
    assert proc.returncode == 2
    assert "unknown flag" in proc.stderr.lower()


def test_missing_token_oneshot(tmp_path):
    """Without AGENT_SERVER_TOKEN, one-shot should fail clean with the
    actual missing-token message — not just anything kara-prefixed."""
    proc = _run(
        ["hello"],
        env_extra={
            "AGENT_SERVER_TOKEN": "",
            "KARA_AGENT": "amos",
            "AGENT_SERVER_URL": "http://127.0.0.1:1",  # unreachable
        },
        timeout=5,
    )
    assert proc.returncode == 1
    assert "kara: AGENT_SERVER_TOKEN not set" in proc.stderr


def test_default_channel_constant():
    """Sanity-check that DEFAULT_CHANNEL reads from KARA_CHANNEL env."""
    src = KARA.read_text()
    assert 'os.environ.get("KARA_CHANNEL", "cli")' in src


# ---------------------------------------------------------------------------
# module-level: import bin/kara directly and drive its functions
# ---------------------------------------------------------------------------

def _import_kara(monkeypatch, token="test-token", **extra_env):
    """Import bin/kara fresh via spec_from_file_location.

    Module-level constants (SERVER_URL, TOKEN, ...) are read from os.environ
    at import time, so env must be set before exec_module. Each call builds
    a brand-new module object (not cached in sys.modules) so tests never
    leak state into one another.
    """
    monkeypatch.setenv("AGENT_SERVER_TOKEN", token)
    for key, val in extra_env.items():
        monkeypatch.setenv(key, val)
    # bin/kara has no .py suffix, so spec_from_file_location can't infer a
    # loader on its own — hand it a SourceFileLoader explicitly.
    loader = importlib.machinery.SourceFileLoader("kara_under_test", str(KARA))
    spec = importlib.util.spec_from_file_location("kara_under_test", KARA, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kara(monkeypatch):
    return _import_kara(monkeypatch)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_http_requires_token(monkeypatch):
    kara = _import_kara(monkeypatch, token="")
    with pytest.raises(kara.ServerError, match="AGENT_SERVER_TOKEN not set"):
        kara._http("GET", "/health")


def test_http_success_sets_auth_header_and_parses_json(kara, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["header"] = req.get_header("Authorization")
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(kara.urlrequest, "urlopen", fake_urlopen)
    result = kara._http("GET", "/health")

    assert result == {"ok": True}
    assert seen["url"] == f"{kara.SERVER_URL}/health"
    assert seen["header"] == "Bearer test-token"


def test_http_wraps_httperror_with_body(kara, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urlerror.HTTPError(
            req.full_url, 404, "Not Found", None,
            io.BytesIO(json.dumps({"error": "no such agent"}).encode()),
        )

    monkeypatch.setattr(kara.urlrequest, "urlopen", fake_urlopen)
    with pytest.raises(kara.ServerError) as exc_info:
        kara._http("GET", "/agents/bogus/reset")
    assert "404" in str(exc_info.value)
    assert "no such agent" in str(exc_info.value)


def test_http_wraps_urlerror_as_unreachable(kara, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urlerror.URLError("connection refused")

    monkeypatch.setattr(kara.urlrequest, "urlopen", fake_urlopen)
    with pytest.raises(kara.ServerError, match="cannot reach"):
        kara._http("GET", "/health")


def test_enqueue_payload_shape(kara, monkeypatch):
    captured = {}

    def fake_http(method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"status": "queued"}

    monkeypatch.setattr(kara, "_http", fake_http)
    message_id = kara._enqueue("amos", "hello world", "cli")

    assert message_id.startswith("cli-")
    assert captured["method"] == "POST"
    assert captured["path"] == "/message"
    body = captured["body"]
    assert body["message_id"] == message_id
    assert body["agent"] == "amos"
    assert body["channel"] == "cli"
    assert body["content"] == "hello world"
    assert body["mentions_agent"] is True
    assert body["channel_id"] == "0"
    assert body["server"] == "local"
    assert "author" in body


def test_enqueue_raises_on_unexpected_response(kara, monkeypatch):
    monkeypatch.setattr(kara, "_http", lambda method, path, body=None: {"status": "error"})
    with pytest.raises(kara.ServerError, match="unexpected enqueue response"):
        kara._enqueue("amos", "hi", "cli")


def test_cmd_health_returns_pretty_json(kara, monkeypatch):
    monkeypatch.setattr(kara, "_http", lambda method, path, body=None: {"status": "ok"})
    out = kara._cmd_health([], "amos", lambda n: None)
    assert json.loads(out) == {"status": "ok"}


def test_cmd_agents_marks_active_agent(kara, monkeypatch):
    monkeypatch.setattr(kara, "_list_agents", lambda: ["amos", "herald"])
    out = kara._cmd_agents([], "herald", lambda n: None)
    assert out.splitlines() == ["  amos", "  herald *"]


def test_cmd_agents_empty_list(kara, monkeypatch):
    monkeypatch.setattr(kara, "_list_agents", lambda: [])
    assert kara._cmd_agents([], "amos", lambda n: None) == "(none)"


def test_cmd_cost_defaults_to_active_agent(kara, monkeypatch):
    calls = []
    monkeypatch.setattr(
        kara, "_http",
        lambda method, path, body=None: calls.append((method, path)) or {"total": 1},
    )
    kara._cmd_cost([], "amos", lambda n: None)
    assert calls == [("GET", "/cost/amos")]


def test_cmd_cost_explicit_target_overrides_active_agent(kara, monkeypatch):
    calls = []
    monkeypatch.setattr(
        kara, "_http",
        lambda method, path, body=None: calls.append((method, path)) or {},
    )
    kara._cmd_cost(["herald"], "amos", lambda n: None)
    assert calls == [("GET", "/cost/herald")]


def test_cmd_reset_posts_to_agent_reset_path(kara, monkeypatch):
    calls = []
    monkeypatch.setattr(
        kara, "_http",
        lambda method, path, body=None: calls.append((method, path)) or {},
    )
    kara._cmd_reset([], "amos", lambda n: None)
    assert calls == [("POST", "/agents/amos/reset")]


def test_cmd_reload_posts_to_explicit_agent_path(kara, monkeypatch):
    calls = []
    monkeypatch.setattr(
        kara, "_http",
        lambda method, path, body=None: calls.append((method, path)) or {},
    )
    kara._cmd_reload(["herald"], "amos", lambda n: None)
    assert calls == [("POST", "/agents/herald/reload")]


def test_cmd_restart_reports_unsupported_off_macos(kara):
    # This box (and CI) is Linux — /restart must say so rather than shelling
    # out to launchctl, which doesn't exist here.
    if sys.platform == "darwin":
        pytest.skip("host is macOS; unsupported-platform branch not reachable")
    out = kara._cmd_restart([], "amos", lambda n: None)
    assert "macOS-only" in out


def test_cmd_agent_no_args_shows_usage(kara):
    switched = []
    out = kara._cmd_agent([], "amos", switched.append)
    assert out == "usage: /agent <name>"
    assert switched == []


def test_cmd_agent_switches_active_agent(kara):
    switched = []
    out = kara._cmd_agent(["herald"], "amos", switched.append)
    assert switched == ["herald"]
    assert out == "active agent: herald"


def test_cmd_help_documents_every_slash_command(kara):
    out = kara._cmd_help([], "amos", lambda n: None)
    for cmd in ("/health", "/agents", "/agent", "/cost", "/reset", "/reload", "/restart", "/quit"):
        assert cmd in out


def test_slash_dispatch_table(kara):
    """Every command in the REPL's dispatch table resolves to the matching
    _cmd_* function, and /help + /? are aliases for the same handler."""
    assert kara.SLASH == {
        "/health": kara._cmd_health,
        "/agents": kara._cmd_agents,
        "/agent": kara._cmd_agent,
        "/cost": kara._cmd_cost,
        "/reset": kara._cmd_reset,
        "/reload": kara._cmd_reload,
        "/restart": kara._cmd_restart,
        "/help": kara._cmd_help,
        "/?": kara._cmd_help,
    }


def test_tail_flushes_final_delta_on_terminal_status():
    """_tail must do one last read after detecting a terminal status so a
    response delta written concurrently with the status update isn't
    dropped on the floor.

    Source-level check: the body of _tail must call _flush_final()
    before returning 'complete' or 'crashed'.
    """
    src = KARA.read_text()
    # Body of _tail
    start = src.index("def _tail(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert "_flush_final()" in body
    # Both terminal-status branches must call it
    assert body.count("_flush_final()") >= 2
