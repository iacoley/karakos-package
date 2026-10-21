#!/usr/bin/env python3
"""Public-safety check: scan text for household PII before it reaches a public repo.

The rules live OUTSIDE the repo (default ~/.karakos/secrets/pii_rules.json) so the
denylist itself is never published. This tool NEVER prints a matched string, a
literal's value or a regex pattern: a finding is `<path>:<line>: <kind> (rule
<section>[<index>])` and nothing more.

Modes (exactly one): --staged | --all | PATH...
Exit: 0 clean (or no rules file), 1 findings, 2 usage/rules error.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

DEFAULT_RULES = "~/.karakos/secrets/pii_rules.json"
ALLOW_FILE = ".public-safety-allow"
MIN_LITERAL = 3
BINARY_PROBE = 8000


def die(msg):
    print(f"public-safety: {msg}", file=sys.stderr)
    sys.exit(2)


def git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if check and r.returncode != 0:
        die(f"git {args[0]} failed")
    return r.stdout


def repo_root():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else os.getcwd()


def load_rules(path):
    """-> list of (compiled, kind, section, index). Never echoes rule content."""
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        die("rules file unreadable or not valid JSON")
    if not isinstance(data, dict):
        die("rules file must be a JSON object")
    rules = []
    for i, item in enumerate(data.get("literals") or []):
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            continue
        value = item["value"]
        if len(value) < MIN_LITERAL:
            continue
        rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.I)
        rules.append((rx, str(item.get("kind") or "pii"), "literals", i))
    for i, item in enumerate(data.get("regexes") or []):
        if not isinstance(item, dict) or not isinstance(item.get("pattern"), str):
            continue
        try:
            rx = re.compile(item["pattern"], re.I)
        except re.error:
            # re.error text can quote the pattern; report the index only.
            die(f"invalid regex in rule regexes[{i}]")
        rules.append((rx, str(item.get("kind") or "pii"), "regexes", i))
    return rules


def load_allow(root):
    """-> list of (glob, kind-or-None)."""
    out = []
    try:
        with open(os.path.join(root, ALLOW_FILE), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        glob, sep, kind = line.rpartition(":")
        if sep and kind.strip() and glob.strip():
            out.append((glob.strip(), kind.strip()))
        else:
            out.append((line.rstrip(":").strip(), None))
    return out


def allowed(path, kind, allow):
    return any(fnmatch.fnmatch(path, g) and (k is None or k == kind) for g, k in allow)


def scan(path, data, rules, allow):
    """-> list of (path, line, kind, section, index). Skips binary content."""
    if b"\x00" in data[:BINARY_PROBE]:
        return []
    text = data.decode("utf-8", errors="replace")
    found = []
    for n, line in enumerate(text.split("\n"), 1):
        for rx, kind, section, idx in rules:
            if rx.search(line) and not allowed(path, kind, allow):
                found.append((path, n, kind, section, idx))
    return found


def iter_inputs(args, root):
    """Yield (display_path, bytes) for the selected mode."""
    if args.staged:
        names = git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], root)
        for p in filter(None, names.decode("utf-8", errors="surrogateescape").split("\0")):
            raw = subprocess.run(["git", "show", f":{p}"], cwd=root, capture_output=True)
            if raw.returncode != 0:
                die("git show failed for a staged file")
            yield p, raw.stdout
    elif args.all:
        names = git(["ls-files", "-z"], root)
        for p in filter(None, names.decode("utf-8", errors="surrogateescape").split("\0")):
            full = os.path.join(root, p)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            try:
                with open(full, "rb") as fh:
                    yield p, fh.read()
            except OSError:
                print(f"public-safety: unreadable file skipped: {p}", file=sys.stderr)
    else:
        for p in args.paths:
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                print(f"public-safety: unreadable file skipped: {p}", file=sys.stderr)
                continue
            rel = os.path.relpath(os.path.abspath(p), root)
            yield (p if rel.startswith("..") else rel), data


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan for household PII using an external rules file.")
    ap.add_argument("paths", nargs="*", metavar="PATH")
    ap.add_argument("--staged", action="store_true", help="scan staged content")
    ap.add_argument("--all", action="store_true", help="scan all tracked files (working tree)")
    ap.add_argument("--rules", help="rules file (else $KARAKOS_PII_RULES, else %s)" % DEFAULT_RULES)
    ap.add_argument("--require-rules", action="store_true", help="exit 2 if the rules file is missing")
    args = ap.parse_args(argv)

    modes = int(args.staged) + int(args.all) + int(bool(args.paths))
    if modes != 1:
        ap.error("exactly one of --staged, --all or PATH... is required")

    rules_path = os.path.expanduser(
        args.rules or os.environ.get("KARAKOS_PII_RULES") or DEFAULT_RULES)
    if not os.path.isfile(rules_path):
        if args.require_rules:
            print("public-safety: rules file required but not found", file=sys.stderr)
            return 2
        print("public-safety: no rules file, skipped", file=sys.stderr)
        return 0

    rules = load_rules(rules_path)
    root = repo_root()
    allow = load_allow(root)
    count = 0
    for path, data in iter_inputs(args, root):
        for p, n, kind, section, idx in scan(path, data, rules, allow):
            print(f"{p}:{n}: {kind} (rule {section}[{idx}])")
            count += 1
    print(f"public-safety: {count} finding(s)", file=sys.stderr)
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main())
