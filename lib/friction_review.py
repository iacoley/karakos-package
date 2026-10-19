"""Friction review (MIRDAIN G6, build B). No LLM, no edits.

Reads data/friction-log.jsonl, groups events by recurring cause, writes a report
to data/friction/YYYY-MM-DD.md and drafts one GitHub issue body per cause. A
draft is never sent by the review itself: `file_issues` needs the caller to
pass an explicit approval and records what it filed so a cause is filed once.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror, request as urlrequest

from lib.friction import read_log

FILED_REL = Path("friction") / "filed.json"
MIN_COUNT = 3
MAX_CAUSES = 10
FIXES = {
    "tool_error": "Make the tool's schema or error text say what a valid call looks like, or add the "
                  "missing case to the tool, so the first call succeeds.",
    "bash_exit": "Add a documented helper or skill for this command, or fix the doc or default that "
                 "leads agents to it.",
    "denied": "Either document the allowed alternative in the agent guide, or, if the block is wrong, "
              "discuss changing the hook with the maintainer.",
    "repeat": "A command run again and again usually wants a skill or a scheduled job; check whether "
              "an existing tool already answers it.",
    "rate_limit": "Review which scheduled work spends the window, and whether the usage governor "
                  "thresholds in config/governor.yaml fit.",
}
_NUM = re.compile(r"\d+")
_QUOTED = re.compile(r"(['\"`])[^'\"`]{1,80}\1")
_PATH = re.compile(r"(?:/[\w.\-]+){2,}")


MIN_DAYS = 2  # file only when a cause shows up on this many separate days


def _norm(detail: str) -> str:
    s = _PATH.sub("<path>", detail.lower())
    s = _QUOTED.sub("<q>", s)
    return _NUM.sub("N", s)[:80].strip()


def cause_id(kind, tool, norm) -> str:
    return hashlib.sha1(f"{kind}|{tool}|{norm}".encode()).hexdigest()[:10]


def group(rows, min_count=MIN_COUNT) -> list:
    """Recurring causes, most frequent first."""
    g = defaultdict(list)
    for r in rows:
        g[(r["kind"], r["tool"], _norm(r.get("detail", "")))].append(r)
    out = []
    for (kind, tool, norm), rs in g.items():
        if len(rs) < min_count:
            continue
        out.append({"id": cause_id(kind, tool, norm), "kind": kind, "tool": tool, "pattern": norm,
                    "count": len(rs), "days": len({str(r.get("ts", ""))[:10] for r in rs}),
                    "agents": sorted({r.get("agent", "?") for r in rs}),
                    "example": rs[-1].get("detail", ""), "first": rs[0].get("ts"),
                    "last": rs[-1].get("ts")})
    out.sort(key=lambda c: (-c["count"], c["id"]))
    return out[:MAX_CAUSES]


def draft_issue(c) -> dict:
    title = f"Friction: {c['kind']} on {c['tool']} ({c['count']}x): {c['pattern'][:50]}"
    body = (f"Found by the friction review (cause `{c['id']}`).\n\n"
            f"- Kind: `{c['kind']}`; tool: `{c['tool']}`\n"
            f"- Seen {c['count']} times on {c['days']} day(s) by {', '.join(c['agents'])} between {c['first']} and {c['last']}\n"
            f"- Example: `{c['example']}`\n\n"
            f"Proposed fix: {FIXES.get(c['kind'], 'Investigate the cause.')}\n\n"
            "Drafted from `data/friction-log.jsonl`; reviewed by a person before filing.\n")
    return {"title": title[:120], "body": body, "cause": c["id"]}


def report_text(causes, days, total) -> str:
    lines = [f"# Friction review, last {days} days", f"{total} events, {len(causes)} recurring cause(s)."]
    for c in causes:
        d = draft_issue(c)
        lines += ["", f"## {d['title']}", f"cause `{c['id']}`", "", d["body"]]
    if not causes:
        lines += ["", "Nothing recurs often enough to propose a fix."]
    return "\n".join(lines) + "\n"


def review(workspace, days=7, min_count=MIN_COUNT, today=None) -> dict:
    rows = read_log(workspace, days)
    causes = group(rows, min_count)
    day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = Path(workspace) / "data" / "friction" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(report_text(causes, days, len(rows)))
    os.replace(tmp, path)
    return {"report": str(path), "events": len(rows), "causes": causes}


def file_issues(workspace, causes, repo, approved, token=None, post=None) -> list:
    """File one issue per cause not filed before. Refuses unless `approved` is True."""
    if approved is not True:
        raise PermissionError("filing needs explicit approval")
    path = Path(workspace) / "data" / FILED_REL
    try:
        filed = json.loads(path.read_text())
    except (OSError, ValueError):
        filed = {}
    token = token or os.environ.get("GITHUB_TOKEN", "")
    post = post or _post
    done = []
    for c in causes:
        if c["id"] in filed or c.get("days", 0) < MIN_DAYS:
            continue  # already filed, or not yet seen on two separate days
        d = draft_issue(c)
        url = post(repo, d["title"], d["body"], token)
        filed[c["id"]] = url
        done.append({"cause": c["id"], "url": url})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(filed, indent=1))
    return done


def _post(repo, title, body, token):
    if not token:
        raise RuntimeError("no GITHUB_TOKEN to file with")
    req = urlrequest.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "karakos-friction-review"})
    try:
        with urlrequest.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())["html_url"]
    except urlerror.HTTPError as e:
        raise RuntimeError(f"GitHub returned HTTP {e.code}") from e
