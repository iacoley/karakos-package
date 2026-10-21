"""Tests for bin/verify-public-safety.py (household PII denylist check)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "verify-public-safety.py"

LITERAL = "Jane Doe"
SHORT = "Jane"
PATTERN = r"203\.0\.113\.\d{1,3}"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "T"], ["config", "user.email", "t@example.com"]):
        subprocess.run(["git", *args], cwd=r, check=True)
    return r


@pytest.fixture
def rules(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({
        "version": 2,
        "schema": "ignored",
        "security_rules": ["ignored"],
        "literals": [
            {"value": "ab", "kind": "short"},         # < 3 chars: skipped
            {"value": SHORT, "kind": "name"},          # literals[1]
            {"value": LITERAL, "kind": "fullname"},    # literals[2]
        ],
        "regexes": [{"pattern": PATTERN, "kind": "ip"}],
    }))
    return p


def run(repo, rules, *args, env=None):
    cmd = [sys.executable, str(SCRIPT), *args]
    if rules is not None:
        cmd += ["--rules", str(rules)]
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)


def out(r):
    return r.stdout + r.stderr


def test_literal_case_insensitive_and_word_bounded(repo, rules):
    (repo / "a.txt").write_text("hello\njane went home\nJANE.\n")
    (repo / "b.txt").write_text("janet went home\nthe ab cd\nxJane\n")
    r = run(repo, rules, "a.txt", "b.txt")
    assert r.returncode == 1
    assert r.stdout.splitlines() == [
        "a.txt:2: name (rule literals[1])",
        "a.txt:3: name (rule literals[1])",
    ]
    assert "2 finding(s)" in r.stderr


def test_regex_match(repo, rules):
    (repo / "c.txt").write_text("ok\nhost 203.0.113.77 up\n203.0.114.1\n")
    r = run(repo, rules, "c.txt")
    assert r.returncode == 1
    assert r.stdout.splitlines() == ["c.txt:2: ip (rule regexes[0])"]


def test_clean_exits_zero(repo, rules):
    (repo / "d.txt").write_text("nothing here\n")
    r = run(repo, rules, "d.txt")
    assert r.returncode == 0
    assert r.stdout == ""
    assert "0 finding(s)" in r.stderr


def test_output_never_leaks_values_or_patterns(repo, rules):
    (repo / "e.txt").write_text(f"{LITERAL} at 203.0.113.9 and jane\n")
    for args in (["e.txt"],):
        r = run(repo, rules, *args)
        assert r.returncode == 1
        text = out(r).lower()
        assert LITERAL.lower() not in text
        assert "203.0.113" not in text
        assert PATTERN.lower() not in text
        assert "jane" not in text
    # invalid regex and bad JSON must not echo rule content either
    bad = repo.parent / "bad.json"
    bad.write_text(json.dumps({"regexes": [{"pattern": "(secretvalue", "kind": "x"}]}))
    r = run(repo, bad, "e.txt")
    assert r.returncode == 2
    assert "secretvalue" not in out(r)
    bad.write_text('{"literals": [{"value": "secretvalue"')
    r = run(repo, bad, "e.txt")
    assert r.returncode == 2
    assert "secretvalue" not in out(r)


def test_staged_reads_index_not_working_tree(repo, rules):
    f = repo / "s.txt"
    f.write_text("clean line\n")
    subprocess.run(["git", "add", "s.txt"], cwd=repo, check=True)
    f.write_text("Jane is here\n")  # unstaged change: must be ignored
    r = run(repo, rules, "--staged")
    assert r.returncode == 0, out(r)

    f.write_text("fine\n")
    g = repo / "t.txt"
    g.write_text("x\nJane is here\n")
    subprocess.run(["git", "add", "s.txt", "t.txt"], cwd=repo, check=True)
    g.write_text("scrubbed after staging\n")  # working tree clean, index dirty
    r = run(repo, rules, "--staged")
    assert r.returncode == 1
    assert r.stdout.splitlines() == ["t.txt:2: name (rule literals[1])"]


def test_all_scans_tracked_files_from_working_tree(repo, rules):
    (repo / "u.txt").write_text("fine\n")
    (repo / "untracked.txt").write_text("Jane\n")
    subprocess.run(["git", "add", "u.txt"], cwd=repo, check=True)
    assert run(repo, rules, "--all").returncode == 0
    (repo / "u.txt").write_text("Jane\n")
    r = run(repo, rules, "--all")
    assert r.returncode == 1
    assert r.stdout.splitlines() == ["u.txt:1: name (rule literals[1])"]


def test_allowlist_by_path_and_by_kind(repo, rules):
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("Jane at 203.0.113.5\n")
    r = run(repo, rules, "docs/x.md")
    assert sorted(r.stdout.splitlines()) == [
        "docs/x.md:1: ip (rule regexes[0])",
        "docs/x.md:1: name (rule literals[1])",
    ]
    # kind-scoped: only the name finding is suppressed
    (repo / ".public-safety-allow").write_text("# c\n\ndocs/*.md:name\n")
    r = run(repo, rules, "docs/x.md")
    assert r.stdout.splitlines() == ["docs/x.md:1: ip (rule regexes[0])"]
    # a different kind does not suppress
    (repo / ".public-safety-allow").write_text("docs/*.md:email\n")
    assert run(repo, rules, "docs/x.md").returncode == 1
    # whole-path suppression
    (repo / ".public-safety-allow").write_text("docs/x.md\n")
    r = run(repo, rules, "docs/x.md")
    assert r.returncode == 0 and r.stdout == ""


def test_missing_rules_file(repo, tmp_path):
    (repo / "m.txt").write_text("Jane\n")
    missing = tmp_path / "nope.json"
    r = run(repo, missing, "m.txt")
    assert r.returncode == 0
    assert "no rules file, skipped" in r.stderr
    r = run(repo, missing, "--require-rules", "m.txt")
    assert r.returncode == 2


def test_rules_from_env(repo, rules):
    import os
    (repo / "n.txt").write_text("Jane\n")
    env = {**os.environ, "KARAKOS_PII_RULES": str(rules)}
    r = run(repo, None, "n.txt", env=env)
    assert r.returncode == 1


def test_binary_file_skipped(repo, rules):
    (repo / "bin.dat").write_bytes(b"\x00\x01Jane Doe 203.0.113.5\n")
    r = run(repo, rules, "bin.dat")
    assert r.returncode == 0
    assert r.stdout == ""


def test_requires_exactly_one_mode(repo, rules):
    assert run(repo, rules).returncode == 2
    assert run(repo, rules, "--staged", "--all").returncode == 2
