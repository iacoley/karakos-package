"""
Tests for the AskUserQuestion Discord surface (#101).

The acceptance test from the issue is `test_acceptance_*` at the bottom of
this file and it is driven end to end: the MCP `ask_user` tool runs in a
worker thread exactly as it does under a real agent, the agent server serves
it over real HTTP through its real routing table, the outbound Discord call
is captured so the embed and its buttons can be inspected, and the click
comes back in through `DiscordAdapter.on_interaction` — the method discord.py
itself dispatches — over a real aiohttp client.

Nothing here asserts on the source text of a call site except where the thing
under test is placement rather than behaviour, and those checks go through
the AST so a comment cannot satisfy them.

What is deliberately NOT here: a test that Claude Code's built-in
AskUserQuestion tool is intercepted. It is not intercepted, because it does
not exist over this transport — `claude -p --input-format stream-json` omits
it from the session tool list even with `--allowedTools AskUserQuestion`
(checked against claude 2.1.220). The bridge is a replacement tool, and these
tests exercise the replacement.
"""

import ast
import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"
TOOLS_SERVER = PACKAGE_ROOT / "mcp" / "tools-server.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")
aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

TOKEN = "test-agent-server-token"
CHANNEL = "555000111"
ASKER_ID = "42"
BYSTANDER_ID = "77"
OWNER_ID = "9001"


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

def _load(name, path, workspace):
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


@pytest.fixture
def workspace(tmp_path):
    for d in ("logs", "data/health/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def ask_handler(workspace):
    return _load("ask_handler_under_test", PACKAGE_ROOT / "bin" / "ask_handler.py", workspace)


@pytest.fixture
def ags(workspace):
    """bin/agent-server.py against a scratch workspace, configured for one agent."""
    module = _load("ags_ask_under_test", AGENT_SERVER, workspace)
    module.AGENT_SERVER_TOKEN = TOKEN
    module.OWNER_DISCORD_ID = OWNER_ID
    module.agent_config = {"amos": {"model": "sonnet"}, "kothar": {}}
    module.AGENT_TOKENS = {"amos": "bot-token-amos"}
    module.agent_turn_context["amos"] = {
        "channel_id": CHANNEL,
        "author_ids": [ASKER_ID],
        "message_ids": ["m1"],
    }
    return module


@pytest.fixture
def relay(workspace):
    return _load("relay_ask_under_test", RELAY_PATH, workspace)


@pytest.fixture
def tools(workspace):
    module = _load("tools_ask_under_test", TOOLS_SERVER, workspace)
    module.AGENT_SERVER_TOKEN = TOKEN
    module.KARAKOS_AGENT = "amos"
    module.ASK_POLL_INTERVAL_SEC = 0.02
    return module


# ---------------------------------------------------------------------------
# Fake Discord REST — captures what the agent server would have posted
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload=None, text=""):
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


class FakeDiscordREST:
    """Records message bodies posted to the Discord API."""

    def __init__(self, status=200):
        self.status = status
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, "json": kwargs.get("json"),
                           "headers": kwargs.get("headers", {})})
        if self.status in (200, 201):
            return FakeResponse(self.status, {"id": f"discord-msg-{len(self.posts)}"})
        return FakeResponse(self.status, text=f"error {self.status}")

    @property
    def last_body(self):
        return self.posts[-1]["json"] if self.posts else None


# ---------------------------------------------------------------------------
# Server harness — the real routing table, over real HTTP
# ---------------------------------------------------------------------------

class Harness:
    def __init__(self, ags, client):
        self.ags = ags
        self.client = client
        self.url = str(client.make_url("")).rstrip("/")

    def auth(self):
        return {"Authorization": f"Bearer {TOKEN}"}

    async def create(self, **body):
        payload = {"agent": "amos", "question": "Tea or coffee?",
                   "options": ["Tea", "Coffee", "Neither"]}
        payload.update(body)
        return await self.client.post("/ask", json=payload, headers=self.auth())

    async def status(self, ask_id):
        return await self.client.get(f"/ask/{ask_id}", headers=self.auth())

    async def answer(self, ask_id, index, user_id=ASKER_ID, user_name="Mike"):
        return await self.client.post(
            f"/ask/{ask_id}/answer",
            json={"index": index, "user_id": user_id, "user_name": user_name},
            headers=self.auth(),
        )


async def _harness(ags, discord_status=200):
    ags.http_session = FakeDiscordREST(discord_status)
    server = TestServer(ags.create_app(with_lifecycle=False))
    client = TestClient(server)
    await client.start_server()
    return Harness(ags, client)


def run(coro_fn):
    """Run an async scenario, closing the test client afterwards."""
    async def wrapper():
        return await coro_fn()
    return asyncio.run(wrapper())


async def wait_until(predicate, timeout=10.0, interval=0.01):
    """Wait on an observable with a deadline. Never a bare sleep-and-hope."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never became true within {timeout}s")


def beacon_state(ags, agent="amos"):
    path = ags.AGENT_BEACON_DIR / f"{agent}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())["state"]


# ---------------------------------------------------------------------------
# The rendered question
# ---------------------------------------------------------------------------

def test_ask_renders_an_embed_with_one_button_per_option(ags):
    async def scenario():
        h = await _harness(ags)
        resp = await h.create(options=["Tea", "Coffee", "Neither"])
        assert resp.status == 201, await resp.text()
        body = await resp.json()

        sent = ags.http_session.last_body
        assert sent is not None, "nothing was posted to Discord"
        assert CHANNEL in ags.http_session.posts[-1]["url"]

        embeds = sent.get("embeds")
        assert embeds and embeds[0]["description"] == "Tea or coffee?"

        buttons = [c for row in sent.get("components", []) for c in row["components"]]
        assert [b["label"] for b in buttons] == ["Tea", "Coffee", "Neither"]
        assert all(b["type"] == 2 for b in buttons), "components must be buttons"
        assert all(row["type"] == 1 for row in sent["components"]), "buttons need action rows"

        for idx, button in enumerate(buttons):
            assert len(button["custom_id"]) <= 100, "Discord caps custom_id at 100 chars"
            assert button["custom_id"].endswith(f":{idx}")
            assert body["ask_id"] in button["custom_id"]
        await h.client.close()

    run(scenario)


def test_more_than_five_options_are_split_across_action_rows(ags):
    """Discord refuses more than five buttons in one row."""
    async def scenario():
        h = await _harness(ags)
        resp = await h.create(options=[f"opt{i}" for i in range(7)])
        assert resp.status == 201
        rows = ags.http_session.last_body["components"]
        assert [len(r["components"]) for r in rows] == [5, 2]
        await h.client.close()

    run(scenario)


def test_the_question_is_posted_under_the_gateway_agents_token(ags):
    """The relay holds one gateway connection, opened with the first agent in
    agents.json that has a token. A question posted under any other token is
    unclickable — Discord has nowhere to deliver the interaction."""
    async def scenario():
        ags.agent_config = {"amos": {}, "kothar": {}}
        ags.AGENT_TOKENS = {"amos": "bot-token-amos", "kothar": "bot-token-kothar"}
        ags.agent_turn_context["kothar"] = {"channel_id": CHANNEL, "author_ids": [ASKER_ID]}
        h = await _harness(ags)
        resp = await h.create(agent="kothar")
        assert resp.status == 201
        auth = ags.http_session.posts[-1]["headers"]["Authorization"]
        assert auth == "Bot bot-token-amos", (
            "kothar's question must go out under the relay's own token or the "
            "button click is never delivered"
        )
        assert ags.http_session.last_body["embeds"][0]["footer"]["text"] == "asked by kothar"
        await h.client.close()

    run(scenario)


def test_a_failed_discord_post_is_reported_not_silently_parked(ags):
    """The old behaviour was a question that went nowhere. A question that
    could not be posted must fail the tool call, not hang it."""
    async def scenario():
        h = await _harness(ags, discord_status=403)
        resp = await h.create()
        assert resp.status == 502
        assert len(ags.ask_registry) == 0, "no ask may be left waiting on a message that does not exist"
        await h.client.close()

    run(scenario)


# ---------------------------------------------------------------------------
# The answer round trip, through the server's real routes
# ---------------------------------------------------------------------------

def test_clicking_an_option_resolves_that_ask_to_that_label(ags):
    async def scenario():
        h = await _harness(ags)
        ask_id = (await (await h.create()).json())["ask_id"]

        pending = await (await h.status(ask_id)).json()
        assert pending["status"] == "pending"
        assert pending["answer"] is None

        result = await (await h.answer(ask_id, 1)).json()
        assert result["outcome"] == "answered"
        assert result["answer"] == "Coffee"

        final = await (await h.status(ask_id)).json()
        assert final["status"] == "answered"
        assert final["answer"] == "Coffee"
        assert final["answer_index"] == 1
        assert final["answered_by"] == "Mike"
        await h.client.close()

    run(scenario)


def test_the_channel_comes_from_the_turn_that_prompted_the_question(ags):
    """process_agent_queue records the channel; /ask has no request context of
    its own. Without that hand-off the question has nowhere to go."""
    async def scenario():
        ags.agent_turn_context["amos"] = {"channel_id": "777", "author_ids": [ASKER_ID]}
        h = await _harness(ags)
        resp = await h.create()
        assert resp.status == 201
        assert "/channels/777/messages" in ags.http_session.posts[-1]["url"]
        await h.client.close()

    run(scenario)


def test_a_turn_with_no_channel_is_refused_rather_than_hung(ags):
    async def scenario():
        ags.agent_turn_context["amos"] = {"channel_id": "0", "author_ids": []}
        h = await _harness(ags)
        resp = await h.create()
        assert resp.status == 409
        assert ags.http_session.posts == []
        await h.client.close()

    run(scenario)


def test_second_click_reports_the_existing_answer(ags):
    async def scenario():
        h = await _harness(ags)
        ask_id = (await (await h.create()).json())["ask_id"]
        await h.answer(ask_id, 0)
        second = await (await h.answer(ask_id, 2)).json()
        assert second["outcome"] == "already"
        assert second["answer"] == "Tea", "a late click must not overwrite the decision"
        await h.client.close()

    run(scenario)


def test_a_bystander_cannot_answer_someone_elses_question(ags):
    """The answer is fed straight back into the agent's context."""
    async def scenario():
        h = await _harness(ags)
        ask_id = (await (await h.create()).json())["ask_id"]
        blocked = await (await h.answer(ask_id, 1, user_id=BYSTANDER_ID)).json()
        assert blocked["outcome"] == "forbidden"

        still = await (await h.status(ask_id)).json()
        assert still["status"] == "pending"

        owner = await (await h.answer(ask_id, 1, user_id=OWNER_ID)).json()
        assert owner["outcome"] == "answered", "the owner may always answer"
        await h.client.close()

    run(scenario)


def test_an_unattended_turn_may_be_answered_by_anyone(ags):
    """A heartbeat has no human author; restricting its question to nobody
    would just hang it."""
    async def scenario():
        ags.agent_turn_context["amos"] = {"channel_id": CHANNEL, "author_ids": []}
        h = await _harness(ags)
        ask_id = (await (await h.create()).json())["ask_id"]
        result = await (await h.answer(ask_id, 0, user_id=BYSTANDER_ID)).json()
        assert result["outcome"] == "answered"
        await h.client.close()

    run(scenario)


def test_an_unknown_ask_gets_an_explanation_not_a_dead_button(ags):
    async def scenario():
        h = await _harness(ags)
        result = await (await h.answer("nosuchask", 0)).json()
        assert result["outcome"] == "unknown"
        assert result["note"], "the clicker must be told something"
        assert (await h.status("nosuchask")).status == 404
        await h.client.close()

    run(scenario)


def test_ask_routes_require_the_bearer_token(ags):
    async def scenario():
        h = await _harness(ags)
        assert (await h.client.post("/ask", json={"agent": "amos"})).status == 401
        assert (await h.client.get("/ask/whatever")).status == 401
        assert (await h.client.post("/ask/whatever/answer", json={})).status == 401
        await h.client.close()

    run(scenario)


def test_a_malformed_question_is_rejected_with_a_reason(ags):
    async def scenario():
        h = await _harness(ags)
        too_many = [f"option {i}" for i in range(11)]  # distinct: not caught as duplicates
        for bad in ({"options": []}, {"options": too_many}, {"question": "   "},
                    {"options": ["Same", "same"]},
                    {"options": [{"description": "no label"}]}):
            resp = await h.create(**bad)
            assert resp.status == 400, f"{bad} should have been rejected"
            assert (await resp.json())["error"]
        assert ags.http_session.posts == [], "nothing malformed reaches Discord"
        await h.client.close()

    run(scenario)


# ---------------------------------------------------------------------------
# Liveness — a person thinking is not a wedged agent
# ---------------------------------------------------------------------------

def test_a_pending_question_is_not_reported_as_a_wedged_agent(ags, workspace):
    """A person takes minutes to decide, during which the subprocess emits
    nothing at all. bin/wedge-check.py must not page for that — and must go
    straight back to watching once the question resolves."""
    wedge = _load("wedge_ask_under_test", PACKAGE_ROOT / "bin" / "wedge-check.py", workspace)

    async def scenario():
        h = await _harness(ags)
        ask_id = (await (await h.create()).json())["ask_id"]

        assert beacon_state(ags) == "AWAITING_USER"
        # Threshold of 0 means "anything silent at all is wedged" — the
        # harshest possible reading, so passing it is about the state and not
        # about timing.
        assert wedge.find_wedged(0) == [], "a pending question read as a wedge"

        await h.answer(ask_id, 0)
        assert beacon_state(ags) == "PROCESSING"
        wedged = wedge.find_wedged(0)
        assert [w["agent"] for w in wedged] == ["amos"], (
            "after the answer the agent must be watchable again"
        )
        await h.client.close()

    run(scenario)


def test_a_question_nobody_answers_gives_the_turn_back(ags, workspace):
    wedge = _load("wedge_ask_under_test2", PACKAGE_ROOT / "bin" / "wedge-check.py", workspace)

    async def scenario():
        h = await _harness(ags)
        ask_id = (await (await h.create(timeout=10)).json())["ask_id"]
        assert beacon_state(ags) == "AWAITING_USER"

        # Reach into the registry rather than sleeping ten seconds: the thing
        # under test is what the *status route* does with an elapsed deadline.
        ags.ask_registry.get(ask_id).expires_at = time.time() - 1

        expired = await (await h.status(ask_id)).json()
        assert expired["status"] == "expired"
        assert beacon_state(ags) == "PROCESSING", (
            "an expired question must hand the agent back to the wedge detector"
        )
        assert wedge.find_wedged(0), "wedge detection stayed disabled after the timeout"

        # The buttons are still on screen after the timeout. Clicking one must
        # not answer a question the agent has already stopped waiting for.
        late = await (await h.answer(ask_id, 1)).json()
        assert late["outcome"] == "expired"
        assert late["answer"] is None
        assert (await (await h.status(ask_id)).json())["status"] == "expired"
        await h.client.close()

    run(scenario)


# ---------------------------------------------------------------------------
# The hand-off from the message loop — driven through process_agent_queue
# ---------------------------------------------------------------------------

class FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        return None


class FakeStdout:
    """Feeds a canned stream-json turn, running a hook on the first read.

    The hook fires at the moment the subprocess would be mid-turn, which is
    the only moment the turn context is supposed to exist.
    """

    def __init__(self, lines, on_first_read):
        self._lines = list(lines)
        self._hook = on_first_read

    async def readline(self):
        if self._hook is not None:
            self._hook()
            self._hook = None
        return self._lines.pop(0) if self._lines else b""


class FakeProc:
    def __init__(self, stdout):
        self.stdin = FakeStdin()
        self.stdout = stdout
        self.pid = 4242


def test_the_turn_context_is_populated_while_the_subprocess_runs(ags):
    """The channel and the entitled answerers reach /ask through this, and
    through nothing else. Asserting it only in the AST would pass against a
    hand-off that is never reached at runtime."""
    seen = {}

    async def scenario():
        ags.http_session = FakeDiscordREST()
        ags.agent_turn_context.pop("amos", None)
        await ags.init_db()
        ags.agent_locks["amos"] = asyncio.Lock()
        ags.agent_states["amos"] = "IDLE"

        await ags.db.execute(
            "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
            " author_id, is_bot, content, message_id, mentions_agent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("amos", "kitchen", CHANNEL, "discord", "Mike", ASKER_ID, 0,
             "decide something", "msg-1", 1),
        )
        await ags.db.commit()

        def mid_turn():
            seen["context"] = dict(ags.agent_turn_context.get("amos") or {})
            # A question raised during the turn must not outlive it.
            ags.ask_registry.create("amos", CHANNEL, "q?", ["a", "b"])
            seen["registry_during"] = len(ags.ask_registry)

        result_event = json.dumps({
            "type": "result", "session_id": "s1", "result": "done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode() + b"\n"
        ags.agent_processes["amos"] = FakeProc(FakeStdout([result_event], mid_turn))

        await ags.process_agent_queue("amos")
        await ags.db.close()

    run(scenario)

    assert seen["context"]["channel_id"] == CHANNEL
    assert seen["context"]["author_ids"] == [ASKER_ID], \
        "the people who prompted the turn are the people allowed to answer"
    assert seen["registry_during"] == 1
    assert len(ags.ask_registry) == 0, \
        "a question outliving its turn would answer into a subprocess that stopped waiting"
    assert "amos" not in ags.agent_turn_context


# ---------------------------------------------------------------------------
# The relay's half — DiscordAdapter.on_interaction
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, user_id=ASKER_ID, name="Mike"):
        self.id = user_id
        self.display_name = name
        self.name = name


class FakeResponseAPI:
    def __init__(self):
        self.edited = None
        self.ephemeral = []

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_message(self, content, ephemeral=False):
        self.ephemeral.append((content, ephemeral))


class FakeMessage:
    def __init__(self, embed=None):
        self.embeds = [embed] if embed is not None else []


class FakeInteraction:
    def __init__(self, custom_id, user_id=ASKER_ID, kind=None, embed=None):
        self.type = kind or discord.InteractionType.component
        self.data = {"custom_id": custom_id}
        self.user = FakeUser(user_id)
        self.response = FakeResponseAPI()
        self.message = FakeMessage(embed)


def make_adapter(relay, url):
    adapter = relay.DiscordAdapter.__new__(relay.DiscordAdapter)
    adapter.http_session = None
    adapter.server_ids = set()
    relay.AGENT_SERVER_URL = url
    relay.AGENT_SERVER_TOKEN = TOKEN
    return adapter


def test_relay_ignores_components_that_are_not_ours(ags, relay):
    """Every component interaction in the guild arrives here."""
    async def scenario():
        h = await _harness(ags)
        adapter = make_adapter(relay, h.url)

        class Boom:
            async def post(self, *a, **k):
                raise AssertionError("a foreign component reached the agent server")

        adapter.http_session = Boom()
        foreign = (
            "some-other-feature",
            # Same three-part shape, different owner. Without a prefix check
            # this one is indistinguishable from ours.
            "poll:abc123:0",
            "kask:onlytwo",
            "kask:abc:notanint",
            "kask::0",
            None,
        )
        for custom_id in foreign:
            interaction = FakeInteraction(custom_id)
            await adapter.on_interaction(interaction)
            assert interaction.response.edited is None
            assert interaction.response.ephemeral == []
        await h.client.close()

    run(scenario)


def test_relay_ignores_non_component_interactions(ags, relay):
    async def scenario():
        h = await _harness(ags)
        adapter = make_adapter(relay, h.url)
        adapter.http_session = aiohttp.ClientSession()
        ask_id = (await (await h.create()).json())["ask_id"]
        interaction = FakeInteraction(
            f"kask:{ask_id}:0", kind=discord.InteractionType.application_command
        )
        await adapter.on_interaction(interaction)
        assert (await (await h.status(ask_id)).json())["status"] == "pending"
        await adapter.http_session.close()
        await h.client.close()

    run(scenario)


def test_relay_strips_the_buttons_and_shows_the_answer(ags, relay):
    async def scenario():
        h = await _harness(ags)
        adapter = make_adapter(relay, h.url)
        adapter.http_session = aiohttp.ClientSession()
        ask_id = (await (await h.create()).json())["ask_id"]

        embed = discord.Embed(title="A question for you", description="Tea or coffee?")
        interaction = FakeInteraction(f"kask:{ask_id}:1", embed=embed)
        await adapter.on_interaction(interaction)

        assert interaction.response.edited is not None, "the message was never updated"
        assert interaction.response.edited["view"] is None, "the buttons must come off"
        answer_fields = [f for f in interaction.response.edited["embed"].fields
                         if f.name == "Answer"]
        assert answer_fields and "Coffee" in answer_fields[0].value
        await adapter.http_session.close()
        await h.client.close()

    run(scenario)


def test_relay_tells_a_bystander_why_nothing_happened(ags, relay):
    async def scenario():
        h = await _harness(ags)
        adapter = make_adapter(relay, h.url)
        adapter.http_session = aiohttp.ClientSession()
        ask_id = (await (await h.create()).json())["ask_id"]

        interaction = FakeInteraction(f"kask:{ask_id}:1", user_id=BYSTANDER_ID)
        await adapter.on_interaction(interaction)

        assert interaction.response.edited is None, (
            "a refused click must not rewrite the question for everyone else"
        )
        assert interaction.response.ephemeral, "the bystander was told nothing"
        note, ephemeral = interaction.response.ephemeral[0]
        assert ephemeral is True
        assert "someone else" in note
        await adapter.http_session.close()
        await h.client.close()

    run(scenario)


# ---------------------------------------------------------------------------
# The MCP tool — what the agent actually calls
# ---------------------------------------------------------------------------

def test_ask_user_is_advertised_by_the_tool_server(tools):
    names = [t["name"] for t in tools.CORE_TOOLS]
    assert "ask_user" in names
    schema = next(t for t in tools.CORE_TOOLS if t["name"] == "ask_user")["inputSchema"]
    assert set(schema["required"]) == {"question", "options"}


def test_a_question_containing_an_ellipsis_is_not_read_as_path_traversal(tools):
    """`..` in prose is not `../`. The shared validator rejected every
    question with an ellipsis in it before this was fixed."""
    schema = next(t for t in tools.CORE_TOOLS if t["name"] == "ask_user")["inputSchema"]
    err = tools.validate_args(
        {"question": "Ship it now, or wait...?", "options": ["Ship", "Wait"]}, schema
    )
    assert err is None, err


def test_path_arguments_are_still_guarded(tools):
    """The opt-out must be per-field, not a hole in the check."""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}
    assert tools.validate_args({"path": "../../etc/passwd"}, schema) is not None


def test_ask_user_without_an_agent_identity_fails_fast(tools):
    tools.KARAKOS_AGENT = ""
    result = tools.ask_user({"question": "q", "options": ["a", "b"]})
    assert result["status"] == "error"
    assert "KARAKOS_AGENT" in result["error"]


def test_ask_user_reports_a_timeout_instead_of_blocking_forever(ags, tools):
    """A turn parked forever on a question nobody answers is the failure the
    poll loop's deadline exists to prevent."""
    async def scenario():
        h = await _harness(ags)
        tools.AGENT_SERVER_URL = h.url

        call = asyncio.create_task(asyncio.to_thread(tools.ask_user, {
            "question": "q", "options": ["a", "b"], "agent": "amos", "timeout": 3600,
        }))

        # Wait for the question to exist (observable: it reached Discord),
        # then move its deadline into the past. No fixed sleep, and no
        # waiting out a real timeout.
        body = await wait_until(lambda: ags.http_session.last_body)
        custom_id = body["components"][0]["components"][0]["custom_id"]
        ask_id, _ = ags.ask_handler.parse_custom_id(custom_id)
        ags.ask_registry.get(ask_id).expires_at = time.time() - 1

        result = await asyncio.wait_for(call, timeout=10)
        assert result["status"] == "timeout"
        assert result["error"]
        await h.client.close()

    run(scenario)


def test_ask_user_gives_up_if_the_server_never_resolves_the_question(ags, tools):
    """The tool's own deadline, distinct from the server's expiry above.

    If the agent server forgets to expire an ask — or is restarted into a
    state where it reports `pending` forever — the poll loop must still end
    the turn. The clock is injected rather than waited out: a fixed sleep
    here would either be flaky or take the full timeout.
    """
    async def scenario():
        h = await _harness(ags)
        tools.AGENT_SERVER_URL = h.url

        ticks = iter([0.0])  # first call sets the deadline; then jump past it

        def fake_monotonic():
            return next(ticks, 1e9)

        def bounded_sleep(_seconds, budget=[6]):
            """A poll loop that never consults its deadline would spin here
            forever. Blow up instead, so the failure is a red test in a
            second rather than a hung suite."""
            budget[0] -= 1
            if budget[0] < 0:
                raise AssertionError("the poll loop never checked its deadline")

        result = await asyncio.to_thread(
            tools.ask_user,
            {"question": "q", "options": ["a", "b"], "agent": "amos", "timeout": 3600},
            bounded_sleep,
            fake_monotonic,
        )
        assert result["status"] == "timeout"
        # The question itself is still pending server-side: this is the
        # client giving up, not the server expiring it.
        assert len(ags.ask_registry) == 1
        await h.client.close()

    run(scenario)


# ---------------------------------------------------------------------------
# The acceptance test from the issue
# ---------------------------------------------------------------------------

def test_acceptance_agent_asks_a_multiple_choice_question_and_gets_the_answer(
        ags, relay, tools):
    """#101, end to end.

    "Ask the agent to put a multiple-choice question to you. Pass = an embed
    with buttons appears; clicking one returns that answer to the agent."

    The agent's half runs in a worker thread because the MCP tool server is a
    synchronous stdio loop — which is exactly how it runs in production, and
    is why the tool blocks the agent's turn until the answer lands.
    """
    async def scenario():
        h = await _harness(ags)
        tools.AGENT_SERVER_URL = h.url
        tools.KARAKOS_AGENT = "amos"

        # 1. The agent calls the tool. Its turn is now blocked.
        pending_call = asyncio.create_task(asyncio.to_thread(tools.ask_user, {
            "question": "Which database should I use for the cache?",
            "options": [
                {"label": "Redis", "description": "fast, another daemon"},
                {"label": "SQLite", "description": "already here"},
            ],
            "header": "Cache backend",
            "timeout": 30,
        }))

        # 2. An embed with buttons appears in Discord.
        body = await wait_until(lambda: ags.http_session.last_body)
        assert body["embeds"][0]["title"] == "Cache backend"
        assert body["embeds"][0]["description"] == "Which database should I use for the cache?"
        buttons = [c for row in body["components"] for c in row["components"]]
        assert [b["label"] for b in buttons] == ["Redis", "SQLite"]
        assert not pending_call.done(), "the tool returned before anyone answered"

        # 3. Someone clicks the second button. The relay carries it back.
        adapter = make_adapter(relay, h.url)
        adapter.http_session = aiohttp.ClientSession()
        embed = discord.Embed(title="Cache backend", description="q")
        interaction = FakeInteraction(buttons[1]["custom_id"], embed=embed)
        await adapter.on_interaction(interaction)
        assert interaction.response.edited is not None

        # 4. That answer is returned to the agent.
        result = await asyncio.wait_for(pending_call, timeout=10)
        assert result["status"] == "answered"
        assert result["answer"] == "SQLite"
        assert result["answer_index"] == 1
        assert result["answered_by"] == "Mike"

        await adapter.http_session.close()
        await h.client.close()

    run(scenario)


# ---------------------------------------------------------------------------
# Wiring that is placement rather than behaviour — checked through the AST
# ---------------------------------------------------------------------------

def _tree(path):
    return ast.parse(path.read_text())


def _function(path, name):
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in {path.name}")


def _assigned_names(func_node):
    """Subscript assignment targets, e.g. `agent_turn_context[agent] = ...`."""
    names = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    names.add(target.value.id)
    return names


def _calls_to(func_node, callee):
    out = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            fn = node.func
            if (getattr(fn, "id", None) or getattr(fn, "attr", None)) == callee:
                out.append(node)
    return out


def test_process_agent_queue_records_the_turn_context():
    """/ask reads this and has no other source for the channel. A grep would
    pass against the comment that explains it."""
    func = _function(AGENT_SERVER, "process_agent_queue")
    assert "agent_turn_context" in _assigned_names(func)
    assert _calls_to(func, "discard_agent"), (
        "questions outliving their turn would answer into a dead subprocess"
    )


def test_the_agent_name_reaches_the_mcp_server_environment():
    """ask_user has no other way to know which agent is calling it."""
    func = _function(AGENT_SERVER, "start_agent_subprocess")
    keys = [k.value for node in ast.walk(func) if isinstance(node, ast.Dict)
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    assert "KARAKOS_AGENT" in keys

    spawn = [n for n in ast.walk(func)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "create_subprocess_exec"]
    assert spawn, "create_subprocess_exec call not found"
    env_kwarg = next((kw for kw in spawn[0].keywords if kw.arg == "env"), None)
    assert env_kwarg is not None and isinstance(env_kwarg.value, ast.Name), (
        "the spawn must actually pass the environment it built"
    )


def test_the_ask_routes_are_registered_on_the_real_app():
    func = _function(AGENT_SERVER, "create_app")
    routes = set()
    for call in _calls_to(func, "add_post") + _calls_to(func, "add_get"):
        if call.args and isinstance(call.args[0], ast.Constant):
            routes.add(call.args[0].value)
    assert {"/ask", "/ask/{ask_id}", "/ask/{ask_id}/answer"} <= routes
    # The routes that were already there must survive the refactor.
    assert {"/message", "/health", "/agents", "/usage"} <= routes


def test_main_serves_the_app_the_tests_mount():
    """If main() built its own router, every route test above would be
    asserting on a table nothing runs."""
    func = _function(AGENT_SERVER, "main")
    assert _calls_to(func, "create_app"), "main() does not use create_app()"
    assert not _calls_to(func, "add_post"), "main() registers routes of its own"


def test_relay_defines_the_event_discordpy_dispatches():
    """The method has to be named on_interaction on the Client subclass or
    discord.py never calls it, however correct its body is."""
    for node in ast.walk(_tree(RELAY_PATH)):
        if isinstance(node, ast.ClassDef) and node.name == "DiscordAdapter":
            methods = {n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert "on_interaction" in methods
            return
    raise AssertionError("DiscordAdapter not found in relay.py")
