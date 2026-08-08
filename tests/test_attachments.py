"""
Tests for Discord attachment delivery (#100).

Acceptance test from the issue: post an image with "what's in this." Pass =
the agent describes it. That splits into three properties, and each one below
is a separate failure that used to happen:

1. The relay downloads the file somewhere the agent can read (it downloaded
   nothing).
2. A message whose entire payload is a file is not rejected as empty (it was
   rejected as "Empty content" and never reached the agent at all).
3. The envelope the agent reads names the file — including when the download
   failed, because "you sent me a 40 MB video I could not open" is a correct
   answer and silence is not.

These drive the real functions against a fake Discord attachment and a real
sqlite database rather than grepping source text: what is under test is what
lands in the envelope and what survives a schema migration, and a substring
search cannot see either.
"""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


def _load(path, module_name, workspace):
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


@pytest.fixture
def relay(tmp_path):
    return _load(RELAY_PATH, "relay_attachments_under_test", tmp_path / "relay-ws")


@pytest.fixture
def ags(tmp_path):
    module = _load(AGENT_SERVER, "ags_attachments_under_test", tmp_path / "ags-ws")
    module.AGENT_TOKENS["amos"] = "fake-token"
    return module


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeAttachment:
    """The parts of discord.Attachment the relay touches."""

    def __init__(self, filename, size=1024, content_type="image/png", body=b"PNGDATA",
                 fail_with=None):
        self.filename = filename
        self.size = size
        self.content_type = content_type
        self._body = body
        self._fail_with = fail_with

    async def save(self, path):
        if self._fail_with:
            raise self._fail_with
        Path(path).write_bytes(self._body)


class FakeMessage:
    def __init__(self, attachments, message_id=4242):
        self.attachments = attachments
        self.id = message_id


def _downloader(relay):
    """The bound method under test, without constructing a Discord client."""
    return relay.DiscordAdapter.download_attachments


# ---------------------------------------------------------------------------
# Filename safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "../../config/agents.json",
    "....//....//etc/passwd",
    "/etc/shadow",
    r"..\..\windows\system32\evil.dll",
])
def test_a_hostile_filename_cannot_escape_the_message_directory(relay, tmp_path, hostile):
    # The uploader picks the filename. The property that matters is not which
    # characters survive but where the write lands: the joined path must stay
    # a direct child of the message's own directory.
    name = relay.safe_attachment_name(hostile, 0)
    resolved = (tmp_path / name).resolve()
    assert resolved.parent == tmp_path.resolve()


def test_backslash_traversal_is_also_neutralised(relay):
    name = relay.safe_attachment_name(r"..\..\windows\system32\evil.dll", 1)
    assert "\\" not in name and "/" not in name


def test_a_name_that_is_only_dots_still_yields_a_usable_component(relay):
    assert relay.safe_attachment_name("..", 0).endswith("attachment")
    assert relay.safe_attachment_name("", 3) == "3-attachment"


def test_two_attachments_with_one_name_do_not_collide(relay):
    first = relay.safe_attachment_name("photo.png", 0)
    second = relay.safe_attachment_name("photo.png", 1)
    assert first != second


def test_a_very_long_name_keeps_its_extension(relay):
    # The extension is how the agent knows it is looking at an image, so the
    # truncation drops the head rather than the tail.
    name = relay.safe_attachment_name("x" * 400 + ".png", 0)
    assert name.endswith(".png")
    assert len(name) <= 100


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def test_an_attachment_is_saved_and_its_real_path_reported(relay):
    message = FakeMessage([FakeAttachment("cat.png", body=b"REALBYTES")])
    result = asyncio.run(_downloader(relay)(None, message))

    assert len(result) == 1
    saved = Path(result[0]["path"])
    assert saved.read_bytes() == b"REALBYTES"
    assert saved.parent == relay.ATTACHMENTS_DIR / "4242"
    assert result[0]["skipped"] is None
    assert result[0]["content_type"] == "image/png"


def test_a_message_with_no_attachments_reports_none(relay):
    assert asyncio.run(_downloader(relay)(None, FakeMessage([]))) == []


def test_an_oversize_attachment_is_announced_rather_than_dropped(relay):
    # The whole complaint in #100 is that a file arrives with no
    # acknowledgement. Refusing to download it is fine; going quiet is not.
    big = FakeAttachment("huge.mov", size=relay.MAX_ATTACHMENT_BYTES + 1)
    result = asyncio.run(_downloader(relay)(None, FakeMessage([big])))

    assert len(result) == 1
    assert result[0]["path"] is None
    assert result[0]["filename"] == "huge.mov"
    assert "limit" in result[0]["skipped"]


def test_a_failed_download_does_not_lose_the_other_attachments(relay):
    attachments = [
        FakeAttachment("broken.png", fail_with=OSError("disk on fire")),
        FakeAttachment("good.png", body=b"OK"),
    ]
    result = asyncio.run(_downloader(relay)(None, FakeMessage(attachments)))

    assert len(result) == 2
    assert result[0]["path"] is None and "disk on fire" in result[0]["skipped"]
    assert Path(result[1]["path"]).read_bytes() == b"OK"


def test_attachments_past_the_per_message_cap_are_counted_not_silently_cut(relay):
    many = [FakeAttachment(f"f{i}.png") for i in range(relay.MAX_ATTACHMENTS_PER_MESSAGE + 3)]
    result = asyncio.run(_downloader(relay)(None, FakeMessage(many)))

    downloaded = [r for r in result if r["path"]]
    assert len(downloaded) == relay.MAX_ATTACHMENTS_PER_MESSAGE
    overflow = result[-1]
    assert "3 more" in overflow["filename"]
    assert overflow["path"] is None


# ---------------------------------------------------------------------------
# Envelope formatting — what the agent actually reads
# ---------------------------------------------------------------------------

def test_no_attachments_formats_to_nothing(ags):
    assert ags.format_attachments(None) == ""
    assert ags.format_attachments("") == ""
    assert ags.format_attachments("[]") == ""
    assert ags.format_attachments([]) == ""


def test_unparseable_column_does_not_break_the_envelope(ags):
    # A malformed row must cost its attachment lines, never the message.
    assert ags.format_attachments("{not json") == ""


def test_a_saved_attachment_puts_its_path_in_the_envelope(ags):
    stored = json.dumps([{
        "filename": "cat.png",
        "content_type": "image/png",
        "size": 2048,
        "path": "/workspace/data/attachments/1/0-cat.png",
        "skipped": None,
    }])
    rendered = ags.format_attachments(stored)

    assert "/workspace/data/attachments/1/0-cat.png" in rendered
    assert "cat.png" in rendered
    assert "image/png" in rendered
    # Without this the agent has a path and no reason to believe it can open it.
    assert "Read tool" in rendered


def test_an_unsaved_attachment_is_still_named_with_its_reason(ags):
    rendered = ags.format_attachments(json.dumps([{
        "filename": "huge.mov",
        "content_type": "video/quicktime",
        "size": 99999999,
        "path": None,
        "skipped": "exceeds the 26214400 byte download limit",
    }]))

    assert "huge.mov" in rendered
    assert "exceeds" in rendered


def test_junk_entries_are_skipped_without_taking_the_good_ones_down(ags):
    rendered = ags.format_attachments(json.dumps([
        "not-a-dict",
        {"filename": "ok.png", "path": "/tmp/ok.png", "size": 1, "content_type": "image/png"},
    ]))
    assert "/tmp/ok.png" in rendered


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def _write_pre_upgrade_database(ags):
    """A message_queue exactly as an install from before this change has it."""
    ags.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ags.DB_PATH)
    con.execute("""
        CREATE TABLE message_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            channel TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            server TEXT DEFAULT 'discord',
            author TEXT NOT NULL,
            author_id TEXT DEFAULT '0',
            is_bot INTEGER DEFAULT 0,
            content TEXT NOT NULL,
            message_id TEXT UNIQUE NOT NULL,
            mentions_agent INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            response TEXT,
            discord_response_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processing_started_at TIMESTAMP,
            processed_at TIMESTAMP
        )
    """)
    con.commit()
    con.close()


def test_upgrading_an_existing_install_gains_the_attachments_column(ags):
    """Driven through `init_db`, not through `ensure_column` directly.

    `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already
    exists, so on every upgraded install the new column arrives *only* if
    init_db actually runs the migration. Calling `ensure_column` from the test
    proves the helper works and proves nothing about whether startup uses it —
    a version of this test that did exactly that passed against an init_db
    with the migration deleted.
    """
    _write_pre_upgrade_database(ags)

    async def run():
        await ags.init_db()
        await ags.db.close()
        # A second startup must not raise "duplicate column name".
        await ags.init_db()
        await ags.db.close()

    asyncio.run(run())

    con = sqlite3.connect(ags.DB_PATH)
    columns = {row[1] for row in con.execute("PRAGMA table_info(message_queue)")}
    con.close()
    assert "attachments" in columns


def test_an_upgraded_install_can_queue_a_message_with_attachments(ags):
    """The consequence of a missed migration, stated as behaviour.

    Without the ALTER, the INSERT in handle_message hits "table message_queue
    has no column named attachments" and every Discord message on that install
    500s — a far worse outcome than the feature merely not working.
    """
    _write_pre_upgrade_database(ags)
    attachment = {"filename": "cat.png", "path": "/w/0-cat.png",
                  "content_type": "image/png", "size": 10, "skipped": None}
    response, rows = _post_message(ags, _payload(attachments=[attachment]))

    assert response.status == 202
    assert json.loads(rows[0]["attachments"])[0]["filename"] == "cat.png"


# ---------------------------------------------------------------------------
# End to end through the real /message handler and the real queue
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, payload, token):
        self._payload = payload
        self.headers = {"Authorization": f"Bearer {token}"}

    async def json(self):
        return self._payload


def _post_message(ags, payload):
    """Run init_db + handle_message against a real database."""
    async def run():
        await ags.init_db()
        ags.agent_config["amos"] = {"system_prompt": "unused"}
        ags.agent_states["amos"] = "BUSY"  # don't spawn a CLI in a unit test
        response = await ags.handle_message(FakeRequest(payload, ags.AGENT_SERVER_TOKEN))
        rows = []
        async with ags.db.execute("SELECT * FROM message_queue") as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        await ags.db.close()
        return response, rows

    return asyncio.run(run())


def _payload(**overrides):
    base = {
        "agent": "amos",
        "channel": "general",
        "channel_id": "1",
        "server": "discord",
        "author": "Mike",
        "author_id": "999",
        "is_bot": False,
        "content": "what's in this",
        "message_id": "m-1",
    }
    base.update(overrides)
    return base


def test_an_image_with_no_caption_is_accepted_rather_than_called_empty(ags):
    # The pre-fix behaviour: content == "" returned 400 Empty content and the
    # message never reached the agent.
    attachment = {"filename": "cat.png", "path": "/w/0-cat.png",
                  "content_type": "image/png", "size": 10, "skipped": None}
    response, rows = _post_message(ags, _payload(content="", attachments=[attachment]))

    assert response.status == 202
    assert len(rows) == 1
    assert json.loads(rows[0]["attachments"])[0]["filename"] == "cat.png"


def test_a_message_with_neither_text_nor_attachments_is_still_rejected(ags):
    response, rows = _post_message(ags, _payload(content=""))
    assert response.status == 400
    assert rows == []


def test_attachments_must_be_a_list(ags):
    response, _ = _post_message(ags, _payload(attachments={"filename": "x"}))
    assert response.status == 400


def test_a_queued_attachment_survives_the_round_trip_into_the_envelope(ags):
    # The join that matters: what handle_message writes is what
    # format_attachments can read back out of the row.
    attachment = {"filename": "receipt.pdf", "path": "/w/data/attachments/9/0-receipt.pdf",
                  "content_type": "application/pdf", "size": 51200, "skipped": None}
    _, rows = _post_message(ags, _payload(attachments=[attachment]))

    rendered = ags.format_attachments(rows[0]["attachments"])
    assert "/w/data/attachments/9/0-receipt.pdf" in rendered
    assert "receipt.pdf" in rendered


def test_a_message_without_attachments_stores_null_and_renders_nothing(ags):
    _, rows = _post_message(ags, _payload())
    assert rows[0]["attachments"] is None
    assert ags.format_attachments(rows[0]["attachments"]) == ""


# ---------------------------------------------------------------------------
# The wiring itself
# ---------------------------------------------------------------------------

def test_the_batch_formatter_actually_calls_format_attachments():
    """Everything above can pass with the envelope never mentioning a file.

    `format_attachments` is correct in isolation and the row round-trips, but
    if `process_agent_queue` never calls it the agent still sees bare text —
    which is the original bug, intact, under a green suite. Checked through
    the AST rather than `in src` so a mention in a comment cannot satisfy it.
    """
    import ast

    tree = ast.parse(AGENT_SERVER.read_text())
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "process_agent_queue"
    )
    called = {
        getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        for n in ast.walk(target) if isinstance(n, ast.Call)
    }
    assert "format_attachments" in called
