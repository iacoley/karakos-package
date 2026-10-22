"""lib/outbox.py: outbound attachment storage and path validation (#277)."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import outbox as ob  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    return ob.open_store(tmp_path / "outbox" / "outbox.db")


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "data" / "outgoing"
    r.mkdir(parents=True)
    return r


def test_enqueue_stores_attachments(conn, root):
    f = root / "a.mp3"
    f.write_bytes(b"x" * 10)
    rid, _ = ob.enqueue(conn, "a", "1", "hi", attachments=[str(f)])
    assert json.loads(ob.get(conn, rid)["attachments"]) == [str(f)]


def test_enqueue_default_has_no_attachments(conn):
    rid, _ = ob.enqueue(conn, "a", "1", "hi")
    assert json.loads(ob.get(conn, rid)["attachments"]) == []


def test_old_store_gets_the_column(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(ob.SCHEMA)
    c.close()
    conn = ob.open_store(p)
    assert "attachments" in {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}


def test_resolve_accepts_file_inside_root(root):
    f = root / "sub" / "a.mp3"
    f.parent.mkdir()
    f.write_bytes(b"abc")
    assert ob.resolve_attachments(["sub/a.mp3"], root) == [str(f.resolve())]
    assert ob.resolve_attachments([str(f)], root) == [str(f.resolve())]


def test_resolve_rejects_traversal(root, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    with pytest.raises(ob.AttachmentError, match="outside"):
        ob.resolve_attachments(["../secret.txt"], root)


def test_resolve_rejects_symlink_escape(root, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    (root / "link").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ob.AttachmentError, match="outside"):
        ob.resolve_attachments(["link"], root)


def test_resolve_rejects_missing_empty_big_and_many(root):
    with pytest.raises(ob.AttachmentError, match="not a file"):
        ob.resolve_attachments(["nope.mp3"], root)
    (root / "e").write_bytes(b"")
    with pytest.raises(ob.AttachmentError, match="empty"):
        ob.resolve_attachments(["e"], root)
    (root / "b").write_bytes(b"xxxx")
    with pytest.raises(ob.AttachmentError, match="too large"):
        ob.resolve_attachments(["b"], root, max_bytes=3)
    with pytest.raises(ob.AttachmentError, match="too many"):
        ob.resolve_attachments(["b", "b"], root, max_count=1)


# --- delivery: bin/agent-server.py ---------------------------------------------
import asyncio  # noqa: E402

from test_outbox_send import FakeDiscord, ags, rows  # noqa: E402,F401


def _row_with(ags, root, names, content="here"):
    paths = []
    for n in names:
        f = root / n
        f.write_bytes(b"data-" + n.encode())
        paths.append(str(f))
    conn = ags._outbox_store()
    rid, _ = ob.enqueue(conn, "amos", "123", content, claimed=True, now=1_000_000.0,
                        chunks_total=1, attachments=paths)
    return rid, ob.get(conn, rid)


def test_send_row_posts_multipart_with_files(ags, tmp_path):
    root = tmp_path / "data" / "outgoing"
    root.mkdir(parents=True, exist_ok=True)
    fake = FakeDiscord()
    seen = {}
    orig = fake.post

    def post(url, **kw):
        seen.update(kw)
        return orig(url, **kw)
    fake.post = post
    ags.http_session = fake
    rid, row = _row_with(ags, root, ["a.mp3"])
    assert asyncio.run(ags.outbox_send_row(row)) == "msg-1"
    assert "json" not in seen and seen["data"] is not None
    assert "Content-Type" not in seen["headers"]
    assert rows(ags)[0]["status"] == "delivered"


def test_send_row_missing_file_is_permanent(ags, tmp_path):
    root = tmp_path / "data" / "outgoing"
    root.mkdir(parents=True, exist_ok=True)
    ags.http_session = FakeDiscord()
    rid, row = _row_with(ags, root, ["a.mp3"])
    (root / "a.mp3").unlink()
    assert asyncio.run(ags.outbox_send_row(row)) is None
    assert ags.http_session.calls == 0
    assert rows(ags)[0]["status"] == "dead"


def test_send_row_without_attachments_still_json(ags):
    fake = FakeDiscord()
    ags.http_session = fake
    conn = ags._outbox_store()
    rid, _ = ob.enqueue(conn, "amos", "123", "plain", claimed=True, now=1_000_000.0, chunks_total=1)
    assert asyncio.run(ags.outbox_send_row(ob.get(conn, rid))) == "msg-1"
    assert fake.bodies[0]["content"] == "plain"


# --- reply markers ------------------------------------------------------------

def test_extract_attachments_strips_markers(root):
    f = root / "a.mp3"
    f.write_bytes(b"x")
    clean, paths, notes = ob.extract_attachments(
        "Here you go\n[[attach: a.mp3]]\n", root)
    assert clean == "Here you go" and paths == [str(f.resolve())] and notes == []


def test_extract_attachments_reports_refusals(root, tmp_path):
    (tmp_path / "s.txt").write_text("s")
    clean, paths, notes = ob.extract_attachments("hi\n[[attach: ../s.txt]]", root)
    assert clean == "hi" and paths == [] and "outside" in notes[0]


def test_extract_ignores_inline_mentions(root):
    clean, paths, notes = ob.extract_attachments("use the [[attach: x]] marker like so", root)
    assert paths == [] and notes == [] and "[[attach: x]]" in clean


def test_post_to_discord_queues_attachments(ags, tmp_path):
    root = tmp_path / "data" / "outgoing"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.mp3").write_bytes(b"x")
    fake = FakeDiscord()
    ags.http_session = fake
    out = asyncio.run(ags.post_to_discord("amos", "123", "listen\n[[attach: a.mp3]]",
                                          dead_letter=True))
    assert out == "msg-1"
    row = rows(ags)[0]
    assert json.loads(row["attachments"]) == [str((root / "a.mp3").resolve())]
    assert "[[attach" not in row["content"]


def test_post_to_discord_attachment_only_reply_has_text(ags, tmp_path):
    root = tmp_path / "data" / "outgoing"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.mp3").write_bytes(b"x")
    ags.http_session = FakeDiscord()
    out = asyncio.run(ags.post_to_discord("amos", "123", "[[attach: a.mp3]]", dead_letter=True))
    assert out == "msg-1" and rows(ags)[0]["content"] == "Attached: a.mp3"


def test_only_the_outgoing_folder_qualifies(ags, tmp_path):
    """The message store sits under data/ but outside data/outgoing/: never sendable."""
    data = tmp_path / "data"
    (data / "outgoing").mkdir(parents=True, exist_ok=True)
    (data / "memory").mkdir(exist_ok=True)
    (data / "memory" / "agent-server.db").write_bytes(b"secrets")
    ags.http_session = FakeDiscord()
    out = asyncio.run(ags.post_to_discord(
        "amos", "123", "here\n[[attach: ../memory/agent-server.db]]\n"
        f"[[attach: {data / 'memory' / 'agent-server.db'}]]", dead_letter=True))
    assert out == "msg-1"
    row = rows(ags)[0]
    assert json.loads(row["attachments"]) == []
    assert row["content"].count("attachment not sent") == 2


def test_total_size_is_capped(root):
    (root / "a").write_bytes(b"xxxx")
    (root / "b").write_bytes(b"xxxx")
    with pytest.raises(ob.AttachmentError, match="together"):
        ob.resolve_attachments(["a", "b"], root, max_bytes=5, max_total=7)
    clean, paths, notes = ob.extract_attachments("hi\n[[attach: a]]\n[[attach: b]]", root,
                                                 max_bytes=5, max_total=7)
    assert len(paths) == 1 and len(notes) == 1 and "together" in notes[0]
    assert clean == "hi"


def test_send_time_recheck_rejects_a_swapped_file(ags, tmp_path):
    root = tmp_path / "data" / "outgoing"
    root.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "data" / "secret.db"
    secret.write_bytes(b"s")
    ags.http_session = FakeDiscord()
    rid, row = _row_with(ags, root, ["a.mp3"])
    (root / "a.mp3").unlink()
    (root / "a.mp3").symlink_to(secret)     # swapped after queueing
    assert asyncio.run(ags.outbox_send_row(row)) is None
    assert ags.http_session.calls == 0
    assert rows(ags)[0]["status"] == "dead"
