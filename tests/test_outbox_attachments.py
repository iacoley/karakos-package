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
    r = tmp_path / "data"
    r.mkdir()
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
