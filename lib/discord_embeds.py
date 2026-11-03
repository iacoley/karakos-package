"""Fenced `embed` blocks in agent replies become Discord embeds. Pure and stdlib only.

An agent reply may contain a fenced block tagged `embed`. The block holds one
embed object, or a JSON array of them:

    ```embed
    {"title": "Build 42", "color": 3066993, "fields": [{"name": "Tests", "value": "pass", "inline": true}]}
    ```

Text outside the blocks is the message content. Only the first 10 objects
count (`EMBEDS_MAX`); the rest are dropped. A block that is not valid JSON, or
whose object has no supported field left after clipping, makes the whole reply
plain text: nothing is dropped. Supported fields: title, description, color
(int), fields[{name, value, inline}], footer (object with text, or a string),
timestamp (ISO 8601) and url (http or https). Unknown keys are dropped.
"""
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# Discord limits (per embed unless noted).
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELDS_MAX = 25
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FOOTER_MAX = 2048
TOTAL_MAX = 6000          # characters across all embeds of one message
EMBEDS_MAX = 10           # embeds per message

# An opening fence at the start of a line, a body, and a closing fence line.
BLOCK_RE = re.compile(r"^[ \t]{0,3}```[ \t]*embed[ \t]*\r?\n(.*?)^[ \t]{0,3}```[ \t]*$",
                      re.M | re.S)


def _text(value: Any, limit: int) -> str:
    """`value` clipped to `limit` characters, or "" when it is not a string or is blank."""
    if not isinstance(value, str):
        return ""
    cut = value[:limit]
    return cut if cut.strip() else ""


def _fields(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value:
        if len(out) >= FIELDS_MAX:
            break
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), FIELD_NAME_MAX)
        val = _text(item.get("value"), FIELD_VALUE_MAX)
        if not (name and val):
            continue
        field: Dict[str, Any] = {"name": name, "value": val}
        if isinstance(item.get("inline"), bool):
            field["inline"] = item["inline"]
        out.append(field)
    return out


def _footer(value: Any) -> Optional[Dict[str, str]]:
    raw = value.get("text") if isinstance(value, dict) else value
    text = _text(raw, FOOTER_MAX)
    return {"text": text} if text else None


def _iso8601(value: str) -> bool:
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value[-1:] in ("Z", "z") else value)
    except ValueError:
        return False
    return True


def _http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def clip_embed(embed: Any) -> Dict[str, Any]:
    """One embed object cut to Discord's limits, with unknown keys dropped.

    Returns {} when nothing supported is left; callers treat that as "not an embed".
    """
    if not isinstance(embed, dict):
        return {}
    out: Dict[str, Any] = {}
    title = _text(embed.get("title"), TITLE_MAX)
    if title:
        out["title"] = title
    description = _text(embed.get("description"), DESCRIPTION_MAX)
    if description:
        out["description"] = description
    color = embed.get("color")
    if isinstance(color, int) and not isinstance(color, bool) and 0 <= color <= 0xFFFFFF:
        out["color"] = color
    fields = _fields(embed.get("fields"))
    if fields:
        out["fields"] = fields
    footer = _footer(embed.get("footer"))
    if footer:
        out["footer"] = footer
    timestamp = embed.get("timestamp")
    if isinstance(timestamp, str) and timestamp and _iso8601(timestamp):
        out["timestamp"] = timestamp
    url = embed.get("url")
    if isinstance(url, str) and _http_url(url):
        out["url"] = url
    return out


def _budget(embed: Dict[str, Any], left: int) -> Tuple[Optional[Dict[str, Any]], int]:
    """`embed` cut to the `left` characters of the message total, and what remains.

    Counted text, in Discord's order: title, description, field names and values,
    footer text. None when nothing counted survives.
    """
    def take(s: str) -> str:
        nonlocal left
        kept = s[:left]
        left -= len(kept)
        return kept if kept.strip() else ""

    out: Dict[str, Any] = {}
    for key in ("title", "description"):
        if key in embed:
            kept = take(embed[key])
            if kept:
                out[key] = kept
    if "fields" in embed:
        fields = []
        for f in embed["fields"]:
            name = take(f["name"])
            value = take(f["value"]) if name else ""
            if name and value:
                fields.append({**f, "name": name, "value": value})
        if fields:
            out["fields"] = fields
    if "footer" in embed:
        kept = take(embed["footer"]["text"])
        if kept:
            out["footer"] = {"text": kept}
    if not any(k in out for k in ("title", "description", "fields", "footer")):
        return None, left
    for key in ("color", "timestamp", "url"):
        if key in embed:
            out[key] = embed[key]
    return out, left


def _parse_block(body: str) -> Optional[List[Dict[str, Any]]]:
    """The clipped embeds of one block, or None if the block is not an embed block."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    items = data if isinstance(data, list) else [data]
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    clipped = [clip_embed(i) for i in items]
    if not all(clipped):
        return None
    return clipped


def extract_embed_blocks(text: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Split a reply into (content, embeds).

    `content` is the reply with the embed blocks removed, stripped; it is "" when
    the reply was only blocks. With no usable embed the reply comes back unchanged
    as (text, []): bad JSON, an object with no supported field, or no blocks at all.
    """
    if not isinstance(text, str) or "```" not in text:
        return text, []
    spans: List[Tuple[int, int]] = []
    objects: List[Dict[str, Any]] = []
    for m in BLOCK_RE.finditer(text):
        parsed = _parse_block(m.group(1))
        if parsed is None:
            return text, []
        spans.append(m.span())
        objects.extend(parsed)
    if not spans:
        return text, []

    embeds: List[Dict[str, Any]] = []
    left = TOTAL_MAX
    for obj in objects[:EMBEDS_MAX]:
        if left <= 0:
            break
        cut, left = _budget(obj, left)
        if cut is not None:
            embeds.append(cut)
    if not embeds:
        return text, []

    pieces, last = [], 0
    for start, end in spans:
        pieces.append(text[last:start])
        last = end
    pieces.append(text[last:])
    content = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()
    return content, embeds


def split_reply(text: Any, enabled: bool = True) -> Tuple[str, List[Dict[str, Any]]]:
    """`extract_embed_blocks` when `enabled`, else the text untouched with no embeds.

    `enabled` is False when the channel has `embeds` off or `suppress_embeds` on.
    """
    if not enabled:
        return text, []
    return extract_embed_blocks(text)
