"""
Tests for delivery-record atomicity in `crash_recovery()`'s unposted sweep.

The case this exists for: the reply was generated, the tokens were spent, and
`post_to_discord()` succeeded — but the row recording that delivery
(`discord_response_id`) was still sitting in an uncommitted transaction when
the process died. The next startup's sweep selects on
`processed = COMPLETE AND discord_response_id IS NULL`, finds those same rows,
and posts the reply to the channel a second time. The recovery path duplicating
exactly what it exists to prevent.

The fix is one line of placement, not logic: `await db.commit()` belongs inside
the loop, next to the UPDATE, so a crash costs the one message in flight rather
than every message posted since the sweep began. Placement is invisible to a
behavioural test that never crashes mid-loop, which is why these are structural.

Like the call-site checks in test_dead_letter.py, these go through the AST
rather than through `in src` — a substring search reads the explanatory comment
above the commit as if it were code, and would pass against the very bug it is
meant to catch.
"""

import ast
import functools
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"


# --- structural helpers ----------------------------------------------------


@functools.lru_cache(maxsize=1)
def _tree():
    """Parsed once, deliberately.

    Every helper here has to hand back nodes from the SAME tree. AST nodes
    compare by identity, so a re-parse per call makes `loop in block` false
    even when it is the very node that block contains -- and
    test_no_batch_commit_after_the_loop, whose whole job is that membership
    test, passed against the unfixed code. Caught by backing the fix out: two
    of three tests failed and the one written for the original bug did not.
    """
    return ast.parse(AGENT_SERVER.read_text())


def _function(name):
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    raise AssertionError(f"{name}() not found in {AGENT_SERVER.name}")


def _is_call_to(node, attr, obj=None):
    """True if `node` is a Call to `obj.attr(...)` (obj optional)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != attr:
        return False
    if obj is None:
        return True
    return isinstance(func.value, ast.Name) and func.value.id == obj


def _contains_call(node, attr, obj=None):
    """True if the subtree rooted at `node` contains a call to `obj.attr`."""
    return any(_is_call_to(n, attr, obj) for n in ast.walk(node))


def _unposted_loop():
    """The `for msg in unposted:` retry loop inside crash_recovery()."""
    recovery = _function("crash_recovery")
    loops = [
        node
        for node in ast.walk(recovery)
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "unposted"
    ]
    assert len(loops) == 1, (
        f"expected exactly one `for ... in unposted` loop in crash_recovery(), "
        f"found {len(loops)} — this test's target moved"
    )
    return loops[0]


def _writes_response_id(stmt):
    """True if `stmt` is ITSELF a db.execute() writing discord_response_id.

    Restricted to simple statements on purpose. Walking a compound statement's
    whole subtree would report the enclosing `if msg["response"]:` as the
    writer too, and then look for the commit as a sibling of that -- i.e. one
    block too far out, which is precisely the placement this file rejects.
    """
    if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                         ast.With, ast.AsyncWith, ast.FunctionDef,
                         ast.AsyncFunctionDef)):
        return False
    for node in ast.walk(stmt):
        if not _is_call_to(node, "execute", "db"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                sql = " ".join(arg.value.split()).upper()
                if "UPDATE" in sql and "DISCORD_RESPONSE_ID" in sql:
                    return True
    return False


# --- the tests -------------------------------------------------------------


def test_unposted_sweep_commits_inside_the_loop():
    """A commit reached only after the last iteration is a batch commit: it
    leaves every message posted earlier in the sweep uncommitted, and a crash
    before it reposts all of them."""
    loop = _unposted_loop()
    assert _contains_call(loop, "commit", "db"), (
        "crash_recovery()'s unposted sweep never commits inside the retry loop "
        "— a crash mid-sweep reposts every message already delivered"
    )


def test_commit_is_a_sibling_of_the_response_id_write():
    """Delivery-record and delivery have to be atomic with each other, not
    merely both-eventually-written. The commit belongs in the same block as the
    UPDATE, so no path can write the id and then skip the commit."""
    loop = _unposted_loop()

    blocks = []
    for node in ast.walk(loop):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list):
                blocks.append(block)

    holding = [b for b in blocks if any(_writes_response_id(s) for s in b)]
    assert holding, (
        "no block inside the retry loop writes discord_response_id — "
        "this test's target moved"
    )

    for block in holding:
        idx = max(i for i, s in enumerate(block) if _writes_response_id(s))
        after = block[idx + 1:]
        assert any(_contains_call(s, "commit", "db") for s in after), (
            "discord_response_id is written without a commit following it in "
            "the same block — the record of delivery is left in an uncommitted "
            "transaction while the delivery itself has already happened"
        )


def test_no_batch_commit_after_the_loop():
    """The original bug, pinned directly: a single `await db.commit()` sitting
    after the loop instead of inside it."""
    recovery = _function("crash_recovery")
    loop = _unposted_loop()

    for node in ast.walk(recovery):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list) or loop not in block:
                continue
            after = block[block.index(loop) + 1:]
            offenders = [s for s in after if _contains_call(s, "commit", "db")]
            assert not offenders, (
                "crash_recovery() commits the unposted sweep in a batch after "
                "the loop (agent-server.py line "
                f"{offenders[0].lineno}) — a crash partway through the sweep "
                "leaves already-posted messages with discord_response_id NULL, "
                "and the next startup reposts them"
            )
