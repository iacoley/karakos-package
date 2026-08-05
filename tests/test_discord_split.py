"""Discord message splitting must never hand the API an oversize chunk.

Discord rejects a message body over 2000 characters with a 400 and the content
is lost. The splitter broke on paragraph and then line boundaries, so a reply
with neither — a long single paragraph, a wall of JSON, a stack trace on one
line — came back as one oversize chunk and never arrived.
"""

import ast
import re
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT

MAX = 2000


def load_splitter(script: str):
    """Pull split_discord_message out of a bin script without booting it.

    Both scripts do heavy work at import time (event loop, sqlite, config),
    so the function is extracted and exec'd on its own.
    """
    src = (PACKAGE_ROOT / "bin" / script).read_text()
    start = src.index("def split_discord_message")
    match = re.search(r"\n(?=(async def |def |# ={10,}))", src[start:])
    body = src[start : start + match.start()]
    ns = {"List": list, "MAX_DISCORD_MSG_LEN": MAX}
    exec(body, ns)
    return ns["split_discord_message"]


SCRIPTS = ["relay.py", "agent-server.py"]


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize(
    "name,text",
    [
        ("wall of text, no breaks at all", "x" * 2500),
        ("long line inside a longer paragraph", "a" * 2400 + "\n" + "b" * 100),
        ("one enormous line", "y" * 9000),
        ("json blob on a single line", '{"k":' + '"v",' * 900 + "}"),
        ("normal prose with paragraphs", ("word " * 100 + "\n\n") * 8),
        ("exactly at the limit", "z" * MAX),
        ("one over the limit", "z" * (MAX + 1)),
    ],
)
def test_no_chunk_exceeds_the_limit(script, name, text):
    chunks = load_splitter(script)(text)
    oversize = [len(c) for c in chunks if len(c) > MAX]
    assert not oversize, f"{name}: chunks over {MAX} chars: {oversize}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_short_text_is_one_chunk(script):
    assert load_splitter(script)("hello") == ["hello"]


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("text", [
    ("paragraph one is quite long. " * 60) + "\n\n" + ("and two. " * 200),
    "x" * 5000,
    ("line\n" * 900),
])
def test_no_content_is_dropped(script, text):
    """Whitespace at a split boundary is consumed; nothing else may be.

    A boundary split eats the newline it cut on, and a hard mid-word split eats
    nothing at all, so the invariant that covers both is character-level and
    whitespace-insensitive.
    """
    chunks = load_splitter(script)(text)
    strip_ws = lambda s: "".join(s.split())
    assert strip_ws("".join(chunks)) == strip_ws(text)


@pytest.mark.parametrize("script", SCRIPTS)
def test_empty_text_produces_no_chunks(script):
    assert load_splitter(script)("") == []


def test_post_to_discord_reports_a_failed_chunk():
    """A chunk that never landed must not be reported as a delivered message.

    Returning a sibling chunk's id tells the caller the whole reply arrived,
    which is how a partial post goes unnoticed.
    """
    src = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    ast.parse(src)
    body = src[src.index("async def post_to_discord") : src.index("async def start_typing")]
    assert "failed += 1" in body
    assert "message is incomplete" in body
    assert "return None" in body.split("if failed:")[1]
