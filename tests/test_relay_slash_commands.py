"""
Tests for the registered Discord application ("slash") commands — issue #86.

Issue #85 shipped *text* interception: the relay reads `/clear` out of a
message body. #86 is a different surface — real application commands
registered over Discord's REST API, delivered as interactions, and dispatched
by `DiscordAdapter.on_interaction`. That entry point is what these tests
drive. Calling `handle_sys_command` directly (which
tests/test_relay_sys_commands.py already does) proves the handler works and
proves nothing about whether a `/` command in the picker ever reaches it,
which is exactly the gap #86 was filed for.

So: every behavioural test here starts at `on_interaction` with an
interaction payload shaped like the one Discord sends, and asserts on what
came out the far end — an HTTP call to the agent server, or text in the
channel.
"""

import ast
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"
REGISTER_PATH = PACKAGE_ROOT / "bin" / "register-discord-commands.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")

OWNER = 4242


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    """Import bin/relay.py with WORKSPACE_ROOT pointed at a temp tree."""
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "logs").mkdir()

    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("relay_slash_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_slash_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


@pytest.fixture(scope="module")
def register_module():
    spec = importlib.util.spec_from_file_location("register_cmds_under_test", REGISTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Doubles — shaped like what discord.py hands an on_interaction listener
# ---------------------------------------------------------------------------

class FakeChannel:
    def __init__(self, channel_id=555):
        self.id = channel_id
        self.name = "general"
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = f"user-{user_id}"


class FakeInteractionResponse:
    def __init__(self, boom=None):
        self.acks = []
        self._boom = boom

    async def send_message(self, content, ephemeral=False):
        if self._boom:
            raise self._boom
        self.acks.append((content, ephemeral))


class FakeInteraction:
    """Mirrors the discord.Interaction surface on_interaction reads."""

    def __init__(self, name, options=None, user_id=OWNER, channel=None,
                 itype=None, ack_error=None):
        self.type = (discord.InteractionType.application_command if itype is None
                     else itype)
        self.data = {"name": name}
        if options is not None:
            self.data["options"] = options
        self.user = FakeUser(user_id)
        self.channel = channel if channel is not None else FakeChannel()
        self.guild = None
        self.id = 987
        self.response = FakeInteractionResponse(boom=ack_error)


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeHttpSession:
    """Records every URL called and hands back a queued response per method."""

    def __init__(self, post=None, get=None):
        self.posts = []
        self.gets = []
        self._post = post
        self._get = get

    def post(self, url, **kwargs):
        self.posts.append(url)
        return self._post if self._post is not None else FakeResponse(200, {})

    def get(self, url, **kwargs):
        self.gets.append(url)
        return self._get if self._get is not None else FakeResponse(200, {"agents": {}})


def make_adapter(relay, http_session):
    """A DiscordAdapter with no Discord connection behind it."""
    adapter = relay.DiscordAdapter.__new__(relay.DiscordAdapter)
    adapter.http_session = http_session
    return adapter


@pytest.fixture
def owner_install(relay, monkeypatch):
    """One configured agent, owner set, no channel defaults."""
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}})
    monkeypatch.setattr(relay, "channels_config", {"channels": {}})


def drive(adapter, interaction):
    """Run the real entry point."""
    asyncio.run(adapter.on_interaction(interaction))


# ---------------------------------------------------------------------------
# Registration <-> dispatch parity
# ---------------------------------------------------------------------------

def test_every_registered_command_has_a_dispatch_entry(relay, register_module):
    """A command in the `/` picker that the relay drops is worse than no
    command: it looks like it worked and did nothing. #86 exists because the
    registered set and the handled set were allowed to be different."""
    registered = {c["name"] for c in register_module.COMMANDS}
    assert registered - relay.SLASH_COMMANDS == set(), (
        "registered with no dispatch entry — these would silently do nothing")
    assert relay.SLASH_COMMANDS - registered == set(), (
        "dispatchable but never registered — these are unreachable from Discord")


def test_the_operational_commands_issue_86_names_are_registered(register_module):
    registered = {c["name"] for c in register_module.COMMANDS}
    for name in ("status", "health", "usage", "cost", "logs",
                 "interrupt", "reload", "clear", "kill", "flush"):
        assert name in registered, f"/{name} is not registered as an application command"


def test_on_interaction_dispatches_through_the_shared_handler():
    """Parsed, not grepped: the two surfaces must stay one implementation, so
    on_interaction has to reach `handle_sys_command` rather than growing its
    own copy of the branches. Docstrings and comments naming the function do
    not count."""
    tree = ast.parse(RELAY_PATH.read_text())
    handler = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_interaction"
    )
    called = {
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "handle_sys_command" in called


def test_text_interception_was_not_widened(relay):
    """#86 wants registered application commands, not more words swallowed out
    of ordinary conversation. SYS_COMMANDS is the text surface and must stay
    the small set #85 shipped."""
    assert relay.SYS_COMMANDS == frozenset({"clear", "reload", "status", "usage"})


# ---------------------------------------------------------------------------
# Entry point: filtering
# ---------------------------------------------------------------------------

def test_non_application_command_interactions_are_ignored(relay, owner_install):
    """Buttons and autocomplete arrive on the same event."""
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction(
        "clear", itype=discord.InteractionType.component)

    drive(adapter, interaction)

    assert http.posts == []
    assert interaction.response.acks == []


def test_unknown_command_name_is_dropped_without_acting(relay, owner_install):
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("rm-rf")

    drive(adapter, interaction)

    assert http.posts == []
    assert interaction.response.acks == []


def test_a_channelless_interaction_is_dropped_rather_than_crashing(relay, owner_install):
    """Every answer goes to a channel. Without one the handler would traceback
    into the log and say nothing in Discord, which is indistinguishable from
    the command never arriving."""
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("status")
    interaction.channel = None

    drive(adapter, interaction)  # must not raise

    assert http.gets == []


def test_non_owner_is_denied_and_reaches_no_endpoint(relay, owner_install):
    """The permission gate lives in the shared handler; this proves the slash
    surface actually goes through it rather than around it."""
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("clear", user_id=999)

    drive(adapter, interaction)

    assert http.posts == []
    assert "Permission denied" in interaction.channel.sent[0]


def test_a_failed_ack_does_not_abort_the_command(relay, owner_install):
    """Discord's 3s ack window is a courtesy, not the command. If the ack
    fails the work still has to happen — otherwise a slow gateway silently
    eats an operator's /interrupt."""
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("reload", ack_error=RuntimeError("gateway sulked"))

    drive(adapter, interaction)

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/amos/reload"]


def test_the_command_is_acknowledged_before_the_work(relay, owner_install):
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("reload")

    drive(adapter, interaction)

    assert interaction.response.acks, "no ack — Discord shows 'did not respond'"
    content, ephemeral = interaction.response.acks[0]
    assert "reload" in content
    assert ephemeral is True


# ---------------------------------------------------------------------------
# Entry point: per-command behaviour
# ---------------------------------------------------------------------------

def test_agent_option_selects_the_target(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}, "kothar": {}})
    monkeypatch.setattr(relay, "channels_config", {"channels": {}})
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction(
        "clear", options=[{"name": "agent", "value": "kothar"}])

    drive(adapter, interaction)

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/kothar/reset"]


def test_channel_default_agent_is_used_when_no_option_given(relay, monkeypatch):
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}, "kothar": {}})
    monkeypatch.setattr(relay, "channels_config", {
        "channels": {"general": {"id": "555", "default_agent": "kothar"}}})
    http = FakeHttpSession()
    adapter = make_adapter(relay, http)

    drive(adapter, FakeInteraction("reload", channel=FakeChannel(555)))

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/kothar/reload"]


def test_interrupt_reports_a_stopped_generation(relay, owner_install):
    http = FakeHttpSession(post=FakeResponse(200, {"status": "interrupted",
                                                   "interrupted": True}))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("interrupt")

    drive(adapter, interaction)

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/amos/interrupt"]
    assert "interrupted" in interaction.channel.sent[0]
    assert "session preserved" in interaction.channel.sent[0]


def test_interrupt_says_so_when_there_was_nothing_running(relay, owner_install):
    """"Stopped it" and "it was already finished" must not read the same — the
    second means your interrupt arrived too late to matter."""
    http = FakeHttpSession(post=FakeResponse(200, {"status": "idle",
                                                   "interrupted": False}))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("interrupt")

    drive(adapter, interaction)

    assert "nothing to interrupt" in interaction.channel.sent[0]


def test_kill_reports_whether_a_subprocess_was_running(relay, owner_install):
    http = FakeHttpSession(post=FakeResponse(200, {"status": "killed",
                                                   "was_running": False}))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("kill")

    drive(adapter, interaction)

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/amos/kill"]
    assert "no subprocess running" in interaction.channel.sent[0]


def test_flush_reports_how_many_messages_were_dropped(relay, owner_install):
    http = FakeHttpSession(post=FakeResponse(200, {"status": "flushed", "flushed": 7}))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("flush")

    drive(adapter, interaction)

    assert http.posts == [f"{relay.AGENT_SERVER_URL}/agents/amos/flush"]
    assert "7 message(s) dropped" in interaction.channel.sent[0]


def test_agent_server_failure_is_reported_not_swallowed(relay, owner_install):
    http = FakeHttpSession(post=FakeResponse(503, text="down"))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("interrupt")

    drive(adapter, interaction)

    assert "failed" in interaction.channel.sent[0]
    assert "503" in interaction.channel.sent[0]


def test_cost_reads_the_endpoint_cost_report_sh_uses(relay, owner_install):
    """bin/cost-report.sh curls GET /cost/<agent>. /cost must read the same
    endpoint or the channel and the CLI report different money."""
    http = FakeHttpSession(get=FakeResponse(
        200, {"agent": "amos", "daily": 3.5, "monthly": 42.25, "session": 0.5}))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("cost")

    drive(adapter, interaction)

    assert http.gets == [f"{relay.AGENT_SERVER_URL}/cost/amos"]
    out = interaction.channel.sent[0]
    assert "today $3.50" in out
    assert "month $42.25" in out


def test_cost_endpoint_matches_the_url_cost_report_sh_builds():
    """Pins the two together from the shell side as well: if cost-report.sh is
    repointed, this fails rather than letting /cost quietly diverge."""
    script = (PACKAGE_ROOT / "bin" / "cost-report.sh").read_text()
    assert 'URL="$AGENT_SERVER_URL/cost/$AGENT"' in script


def test_status_reports_agent_and_subprocess_state_in_channel(relay, owner_install):
    payload = {"agents": {
        "amos": {"state": "PROCESSING", "alive": True, "queue_depth": 2},
    }}
    http = FakeHttpSession(get=FakeResponse(200, payload))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("status")

    drive(adapter, interaction)

    assert http.gets == [f"{relay.AGENT_SERVER_URL}/health"]
    out = interaction.channel.sent[0]
    assert "amos" in out and "PROCESSING" in out and "alive" in out and "queue 2" in out


def test_usage_reports_rate_limit_headroom_in_channel(relay, owner_install):
    payload = {"agents": {"amos": {"summary": "62% of the 5h window"}}}
    http = FakeHttpSession(get=FakeResponse(200, payload))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("usage")

    drive(adapter, interaction)

    assert http.gets == [f"{relay.AGENT_SERVER_URL}/usage"]
    assert "62% of the 5h window" in interaction.channel.sent[0]


def test_help_lists_the_commands_that_are_actually_dispatched(relay, owner_install):
    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("help")

    drive(adapter, interaction)

    out = interaction.channel.sent[0]
    for name in relay.SLASH_COMMANDS:
        assert f"/{name}" in out


def test_untargeted_commands_do_not_demand_an_agent(relay, monkeypatch):
    """With several agents configured and none named, an agent-targeted command
    correctly refuses to guess. /status, /health, /usage, /logs and /help are
    not about one agent, so that refusal must not reach them."""
    monkeypatch.setattr(relay, "OWNER_DISCORD_ID", OWNER)
    monkeypatch.setattr(relay, "agent_config", {"amos": {}, "kothar": {}})
    monkeypatch.setattr(relay, "channels_config", {"channels": {}})
    http = FakeHttpSession(get=FakeResponse(200, {"agents": {"amos": {"summary": "ok"}}}))
    adapter = make_adapter(relay, http)
    interaction = FakeInteraction("usage")

    drive(adapter, interaction)

    assert "Which agent?" not in interaction.channel.sent[0]
    assert http.gets == [f"{relay.AGENT_SERVER_URL}/usage"]


# ---------------------------------------------------------------------------
# /logs — reads real files off disk
# ---------------------------------------------------------------------------

@pytest.fixture
def logs_workspace(relay, monkeypatch, tmp_path):
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(relay, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


def test_logs_returns_the_last_n_lines_of_the_named_log(relay, owner_install, logs_workspace):
    """The acceptance test from #86, verbatim: `/logs relay 40` returns the
    last 40 lines of the relay log."""
    written = [f"line {i}" for i in range(1, 201)]
    (logs_workspace / "logs" / "relay.log").write_text("\n".join(written) + "\n")

    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("logs", options=[
        {"name": "service", "value": "relay"},
        {"name": "lines", "value": 40},
    ])

    drive(adapter, interaction)

    out = "\n".join(interaction.channel.sent)
    assert "line 200" in out
    assert "line 161" in out, "fewer than 40 lines came back"
    assert "line 160" not in out, "more than 40 lines came back"


def test_logs_defaults_to_forty_lines(relay, owner_install, logs_workspace):
    (logs_workspace / "logs" / "relay.log").write_text(
        "\n".join(f"line {i}" for i in range(1, 101)) + "\n")
    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("logs", options=[{"name": "service", "value": "relay"}])

    drive(adapter, interaction)

    out = "\n".join(interaction.channel.sent)
    assert "line 61" in out and "line 60" not in out


def test_logs_caps_the_line_count(relay, owner_install, logs_workspace):
    """An unbounded tail is a way to make the bot post a hundred times."""
    (logs_workspace / "logs" / "relay.log").write_text(
        "\n".join(f"line {i}" for i in range(1, 1001)) + "\n")
    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("logs", options=[
        {"name": "service", "value": "relay"},
        {"name": "lines", "value": 5000},
    ])

    drive(adapter, interaction)

    out = "\n".join(interaction.channel.sent)
    assert f"line {1000 - relay.LOG_LINES_MAX + 1}" in out
    assert f"line {1000 - relay.LOG_LINES_MAX}" not in out


def test_logs_refuses_a_path_outside_the_logs_directory(relay, owner_install, logs_workspace):
    """The service name becomes a path segment. A traversal must be refused,
    not sanitised into some other file's contents."""
    secret = logs_workspace / "config" / "secrets.log"
    secret.parent.mkdir()
    secret.write_text("AGENT_SERVER_TOKEN=hunter2\n")
    (logs_workspace / "logs" / "relay.log").write_text("safe\n")

    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("logs", options=[
        {"name": "service", "value": "../config/secrets"}])

    drive(adapter, interaction)

    out = "\n".join(interaction.channel.sent)
    assert "hunter2" not in out
    assert "Unknown log" in out


def test_logs_names_the_available_logs_when_the_name_is_wrong(relay, owner_install, logs_workspace):
    (logs_workspace / "logs" / "relay.log").write_text("x\n")
    (logs_workspace / "logs" / "agent-server.log").write_text("y\n")
    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("logs", options=[{"name": "service", "value": "nope"}])

    drive(adapter, interaction)

    out = "\n".join(interaction.channel.sent)
    assert "`relay`" in out and "`agent-server`" in out


# ---------------------------------------------------------------------------
# /health — runs the real health monitor
# ---------------------------------------------------------------------------

@pytest.fixture
def health_workspace(relay, monkeypatch, tmp_path):
    """A workspace the real bin/health-monitor.py can be pointed at."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "data" / "health").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    # A copy, so the subprocess is the production script, resolved through the
    # same WORKSPACE_ROOT/bin path the relay uses in the container.
    (tmp_path / "bin" / "health-monitor.py").write_text(
        (PACKAGE_ROOT / "bin" / "health-monitor.py").read_text())
    monkeypatch.setattr(relay, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


def _write_health(workspace, component, age_seconds):
    from datetime import datetime, timedelta
    stamp = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    (workspace / "data" / "health" / component).write_text(
        json.dumps({"timestamp": stamp, "status": "healthy"}))


def test_health_reports_the_monitors_verdict_when_everything_is_fresh(
        relay, owner_install, health_workspace):
    for component in ("mcp-tools.json", "relay.json", "memory.json", "scheduler.json"):
        _write_health(health_workspace, component, age_seconds=1)

    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("health")

    drive(adapter, interaction)

    out = "\n".join(interaction.channel.sent)
    assert "All components healthy" in out
    assert "relay.json" in out


def test_health_reports_a_stale_component(relay, owner_install, health_workspace):
    """The verdict has to be the monitor's own, thresholds and all — a stale
    relay heartbeat is the thing /health exists to surface."""
    for component in ("mcp-tools.json", "memory.json", "scheduler.json"):
        _write_health(health_workspace, component, age_seconds=1)
    _write_health(health_workspace, "relay.json", age_seconds=99999)

    adapter = make_adapter(relay, FakeHttpSession())
    interaction = FakeInteraction("health")

    drive(adapter, interaction)

    out = "\n".join(interaction.channel.sent)
    assert "Health check failures" in out
    assert "relay.json" in out and "stale" in out


def test_health_does_not_alert_the_signals_channel(relay, owner_install, health_workspace):
    """--check is read-only. An operator asking twice must not page twice."""
    monitor = (PACKAGE_ROOT / "bin" / "health-monitor.py").read_text()
    tree = ast.parse(monitor)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    # The --check early return must come before any poke_signals call.
    returns = [n.lineno for n in ast.walk(main_fn) if isinstance(n, ast.Return)]
    pokes = [n.lineno for n in ast.walk(main_fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "poke_signals"]
    assert pokes, "health-monitor no longer alerts at all"
    assert min(returns) < min(pokes)
