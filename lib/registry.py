#!/usr/bin/env python3
"""Agent registry: the one parser for ``config/agents.yaml`` (schema version 2).

    from registry import load_registry
    reg = load_registry(workspace)        # raises RegistryError listing every problem
    reg.primary(); reg.monitor(); reg.shards(); reg.legacy_view()

CLI:  python3 lib/registry.py [--workspace DIR] ids | field <id> <key> | role <role> | legacy | validate
      python3 lib/registry.py init --workspace DIR --primary-id ID --primary-name NAME [...]

Keying rule: a shard ``id`` is the runtime key everywhere (queue, sessions, cost
rows, states, locks). The default shard of agent ``X`` has id ``X``. Account-level
resources (rate-limit state) are never keyed by agent or shard.

This module only reads (and, via ``write_agent``, appends to) agents.yaml. It
never mutates 1.x data.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REGISTRY_VERSION = 2
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ROLES = ("primary", "monitor", "builder", "reviewer", "custom")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
RESET_MODES = ("reset", "compact")
CONTEXT_BUDGET_MIN = 20000
CONTEXT_BUDGET_WARN_ABOVE = 1000000
# handoff_on_reset when the key is not written: the roles that carry a
# long-lived conversation. An explicit value always wins (step 2.6).
HANDOFF_DEFAULT_ROLES = ("primary", "custom")

# key -> (default, type-check description). Order is the canonical key order.
_DEFAULTS = {
    "model": "sonnet",
    "effort": None,
    "max_turns": 200,
    "timeout": 10800,
    "token_budget_4h": None,
    "token_budget_min_pause_s": 1800,
    "context_budget_tokens": None,
    "handoff_on_reset": False,   # effective default is by role, see HANDOFF_DEFAULT_ROLES
    "reset_mode": "reset",
    "system_prompt": None,
    "prompt": None,
    "tool_streaming": True,
    "stream_to_channel": False,
    "dashboard_chat": True,
    "allowed_tools": [],
    "disallowed_tools": [],
    "env": {},
    "label": None,
    "work_stealing": {"enabled": False, "after_s": 5, "max_rows": 5},
    "steering": {"enabled": True, "coalesce_ms": 300, "max_lines_per_turn": 8},
    "send": {"channels": [], "per_hour": 20, "unlimited_channels": []},   # cross-channel send (#278): off until channels are listed
}
_KNOWN_KEYS = {"name", "role", "shards", "discord"} | set(_DEFAULTS)

# Keys copied verbatim into legacy_view() when explicitly set.
_LEGACY_PASSTHROUGH = (
    "system_prompt", "prompt", "model", "max_turns", "timeout", "tool_streaming",
    "stream_to_channel", "dashboard_chat", "allowed_tools", "disallowed_tools", "env",
    "label", "token_budget_4h", "token_budget_min_pause_s", "work_stealing",
    "context_budget_tokens", "reset_mode", "steering", "effort", "send",
)


class RegistryError(Exception):
    """Invalid registry. ``problems`` holds every error found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("invalid agent registry:\n  - " + "\n  - ".join(self.problems))


@dataclass(frozen=True)
class Shard:
    id: str
    agent: str
    channels: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "channels", tuple(self.channels))


@dataclass
class Agent:
    id: str
    name: str
    role: str
    settings: dict            # every schema key with defaults applied
    explicit: dict            # keys exactly as written in the file
    discord: dict = field(default_factory=dict)
    shard_defs: list = field(default_factory=list)   # [(shard_id, [channels])]

    def get(self, key, default=None):
        if key in ("name", "role"):
            return getattr(self, key)
        if key == "discord":
            return self.discord
        return self.settings.get(key, default)


# --------------------------------------------------------------------------
# parsing / validation
# --------------------------------------------------------------------------

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _check_type(aid, key, val, errors, warnings=None):
    p = f"agent '{aid}': '{key}'"
    if key == "model" and not (isinstance(val, str) and val):
        errors.append(f"{p} must be a non-empty string")
    elif key == "effort" and val is not None and val not in EFFORTS:
        errors.append(f"{p} must be one of {', '.join(EFFORTS)} or null")
    elif key in ("max_turns", "timeout") and not (_is_int(val) and val > 0):
        errors.append(f"{p} must be a positive integer")
    elif key == "token_budget_4h" and val is not None \
            and not (_is_int(val) and val >= 1000):
        errors.append(f"{p} must be an integer of at least 1000, or null")
    elif key == "token_budget_min_pause_s" \
            and not (_is_int(val) and 60 <= val <= 21600):
        errors.append(f"{p} must be an integer from 60 to 21600")
    elif key == "context_budget_tokens" and val is not None \
            and not (_is_int(val) and val >= CONTEXT_BUDGET_MIN):
        errors.append(f"{p} must be an integer of at least {CONTEXT_BUDGET_MIN}, or null")
    elif key == "reset_mode" and val not in RESET_MODES:
        errors.append(f"{p} must be one of {', '.join(RESET_MODES)}")
    elif key in ("handoff_on_reset", "tool_streaming", "stream_to_channel",
                 "dashboard_chat") and not isinstance(val, bool):
        errors.append(f"{p} must be true or false")
    elif key in ("system_prompt", "label") and val is not None and not isinstance(val, str):
        errors.append(f"{p} must be a path string")
    elif key == "prompt" and val is not None:
        if not isinstance(val, dict):
            errors.append(f"{p} must be a mapping (section, core, house_style)")
        else:
            for k, v in val.items():
                if k == "section" and not (v is None or isinstance(v, str)):
                    errors.append(f"{p}.section must be a path string")
                elif k in ("core", "house_style") and not isinstance(v, bool):
                    errors.append(f"{p}.{k} must be true or false")
                elif k not in ("section", "core", "house_style"):
                    errors.append(f"{p}: unknown key '{k}'")
    elif key == "work_stealing":
        if not isinstance(val, dict):
            errors.append(f"{p} must be a mapping (enabled, after_s, max_rows)")
        else:
            for k, v in val.items():
                if k == "enabled" and not isinstance(v, bool):
                    errors.append(f"{p}.enabled must be true or false")
                elif k == "after_s" and not (isinstance(v, (int, float))
                                             and not isinstance(v, bool)
                                             and 0 <= v <= 300):
                    errors.append(f"{p}.after_s must be a number from 0 to 300")
                elif k == "max_rows" and not (_is_int(v) and 1 <= v <= 20):
                    errors.append(f"{p}.max_rows must be an integer from 1 to 20")
                elif k not in ("enabled", "after_s", "max_rows"):
                    if warnings is not None:
                        warnings.append(f"agent '{aid}': unknown key 'work_stealing.{k}'")
    elif key == "steering":
        if not isinstance(val, dict):
            errors.append(f"{p} must be a mapping (enabled, coalesce_ms, max_lines_per_turn)")
        else:
            for k, v in val.items():
                if k == "enabled" and not isinstance(v, bool):
                    errors.append(f"{p}.enabled must be true or false")
                elif k == "coalesce_ms" and not (isinstance(v, (int, float))
                                                 and not isinstance(v, bool)
                                                 and 0 <= v <= 5000):
                    errors.append(f"{p}.coalesce_ms must be a number from 0 to 5000")
                elif k == "max_lines_per_turn" and not (_is_int(v) and 1 <= v <= 50):
                    errors.append(f"{p}.max_lines_per_turn must be an integer from 1 to 50")
                elif k not in ("enabled", "coalesce_ms", "max_lines_per_turn"):
                    if warnings is not None:
                        warnings.append(f"agent '{aid}': unknown key 'steering.{k}'")
    elif key == "send":
        if not isinstance(val, dict):
            errors.append(f"{p} must be a mapping (channels, per_hour, unlimited_channels)")
        else:
            for k, v in val.items():
                if k == "channels":
                    ok = isinstance(v, list) and all(
                        not isinstance(x, bool) and isinstance(x, (str, int))
                        and str(x).strip().isdigit() and 15 <= len(str(x).strip()) <= 22
                        for x in v)
                    if not ok:
                        errors.append(f"{p}.channels must be a list of Discord channel ids "
                                      f"(quote them in YAML)")
                elif k == "per_hour":
                    if not (_is_int(v) and 1 <= v <= 200):
                        errors.append(f"{p}.per_hour must be an integer from 1 to 200")
                elif k == "unlimited_channels":
                    ids = [str(x).strip() for x in v] if isinstance(v, list) else None
                    chans = val.get("channels")
                    listed = ([str(x).strip() for x in chans]
                              if isinstance(chans, list) else [])
                    if ids is None or any(isinstance(x, bool) for x in v):
                        errors.append(f"{p}.unlimited_channels must be a list of channel ids")
                    elif any(i not in listed for i in ids):
                        errors.append(f"{p}.unlimited_channels may only list ids that are "
                                      f"also in {p}.channels")
                elif warnings is not None:
                    warnings.append(f"agent '{aid}': unknown key 'send.{k}'")
    elif key in ("allowed_tools", "disallowed_tools") and not _is_str_list(val):
        errors.append(f"{p} must be a list of strings")
    elif key == "env" and not (isinstance(val, dict)
                               and all(isinstance(k, str) for k in val)):
        errors.append(f"{p} must be a mapping of names to values")


class Registry:
    def __init__(self, agents, warnings=None):
        self._agents = dict(agents)          # insertion order == file order
        self.warnings = list(warnings or [])

    # -- lookups ----------------------------------------------------------
    def agents(self):
        return list(self._agents.values())

    def agent(self, agent_id):
        return self._agents[agent_id]

    def ids(self):
        return list(self._agents)

    def by_role(self, role):
        return [a for a in self._agents.values() if a.role == role]

    def primary(self):
        return self.by_role("primary")[0]

    def monitor(self):
        return self.by_role("monitor")[0]

    def shards(self):
        out = []
        for a in self._agents.values():
            out.extend(Shard(sid, a.id, chans) for sid, chans in a.shard_defs)
        return out

    def shards_of(self, agent_id):
        return [s for s in self.shards() if s.agent == agent_id]

    def shard_for_channel(self, name):
        for s in self.shards():
            if name in s.channels:
                return s
        return None

    # -- compatibility ----------------------------------------------------
    def legacy_view(self):
        """The old ``{"agents": {id: {...1.x keys}}}`` dict. Only keys that were
        explicitly set appear, so a registry migrated from a 1.x file yields
        that file's dict back unchanged."""
        out = {}
        for a in self._agents.values():
            entry = {}
            for key in _LEGACY_PASSTHROUGH:
                if key in a.explicit:
                    val = a.explicit[key]
                    entry[key] = list(val) if isinstance(val, list) else \
                        dict(val) if isinstance(val, dict) else val
            # The one value the server reads: explicit wins, else by role.
            entry["handoff_on_reset"] = bool(a.settings["handoff_on_reset"])
            if a.discord.get("token_env"):
                entry["discord_bot_token_env"] = a.discord["token_env"]
            if a.discord.get("bot_id_env"):
                entry["discord_bot_id_env"] = a.discord["bot_id_env"]
            if a.discord.get("role_id_env"):
                entry["discord_role_id_env"] = a.discord["role_id_env"]
            out[a.id] = entry
        return {"agents": out}


def _channel_names(workspace):
    path = Path(workspace) / "config" / "channels.json"
    if not path.exists():
        return set(), None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return set(), f"config/channels.json unreadable: {exc}"
    chans = data.get("channels", {}) if isinstance(data, dict) else {}
    return (set(chans) if isinstance(chans, dict) else set()), None


def parse_registry(data, channel_names=None):
    """Validate a parsed agents.yaml mapping. Returns a Registry or raises
    RegistryError carrying every problem. ``channel_names=None`` skips the
    channel-existence check."""
    errors, warnings = [], []
    if not isinstance(data, dict):
        raise RegistryError(["agents.yaml must be a mapping"])
    if data.get("version") != REGISTRY_VERSION:
        errors.append(f"version must be {REGISTRY_VERSION} (got {data.get('version')!r})")
    for k in data:
        if k not in ("version", "agents"):
            warnings.append(f"unknown top-level key '{k}'")
    raw = data.get("agents")
    if not isinstance(raw, dict) or not raw:
        errors.append("'agents' must be a non-empty mapping")
        raise RegistryError(errors)

    agents = {}
    for aid, body in raw.items():
        aid = str(aid)
        if not ID_RE.match(aid):
            errors.append(f"agent id '{aid}' must match {ID_RE.pattern}")
        if not isinstance(body, dict):
            errors.append(f"agent '{aid}' must be a mapping")
            continue
        for k in body:
            if k not in _KNOWN_KEYS:
                warnings.append(f"agent '{aid}': unknown key '{k}'")
        name = body.get("name")
        if not (isinstance(name, str) and name.strip()):
            errors.append(f"agent '{aid}': 'name' is required")
            name = aid
        role = body.get("role")
        if role is None:
            errors.append(f"agent '{aid}': 'role' is required")
        elif role not in ROLES:
            errors.append(f"agent '{aid}': role '{role}' must be one of {', '.join(ROLES)}")
        settings, explicit = {}, {}
        for key, default in _DEFAULTS.items():
            if key in body:
                _check_type(aid, key, body[key], errors, warnings)
                explicit[key] = body[key]
                settings[key] = body[key]
            else:
                settings[key] = type(default)(default) if isinstance(default, (list, dict)) \
                    else default
        if "handoff_on_reset" not in body:
            settings["handoff_on_reset"] = role in HANDOFF_DEFAULT_ROLES
        cb = body.get("context_budget_tokens")
        if _is_int(cb) and cb > CONTEXT_BUDGET_WARN_ABOVE:
            warnings.append(f"agent '{aid}': context_budget_tokens {cb} is above "
                            f"{CONTEXT_BUDGET_WARN_ABOVE}, larger than any model window")
        if role == "monitor" and body.get("token_budget_4h") is not None:
            errors.append(f"agent '{aid}': a monitor cannot have a token budget "
                          f"(a paused monitor could not report the pause)")
        disc = body.get("discord") or {}
        if not isinstance(disc, dict):
            errors.append(f"agent '{aid}': 'discord' must be a mapping")
            disc = {}
        else:
            for k, v in disc.items():
                if k not in ("token_env", "bot_id_env", "role_id_env"):
                    warnings.append(f"agent '{aid}': unknown key 'discord.{k}'")
                elif not isinstance(v, str):
                    errors.append(f"agent '{aid}': 'discord.{k}' must be a string")
        shard_defs = []
        raw_shards = body.get("shards")
        if raw_shards is None:
            shard_defs = [(aid, [])]
        elif not isinstance(raw_shards, list) or not raw_shards:
            errors.append(f"agent '{aid}': 'shards' must be a non-empty list")
        else:
            for i, s in enumerate(raw_shards):
                if not isinstance(s, dict) or not isinstance(s.get("id"), str):
                    errors.append(f"agent '{aid}': shards[{i}] needs a string 'id'")
                    continue
                chans = s.get("channels", [])
                if not _is_str_list(chans):
                    errors.append(f"agent '{aid}': shard '{s['id']}' channels must be a list of names")
                    chans = []
                for k in s:
                    if k not in ("id", "channels"):
                        warnings.append(f"agent '{aid}': shard '{s['id']}': unknown key '{k}'")
                shard_defs.append((s["id"], list(chans)))
        agents[aid] = Agent(aid, name, role if role in ROLES else "custom", settings,
                            explicit, {k: v for k, v in disc.items() if isinstance(v, str)},
                            shard_defs)

    # -- cross-agent rules ---------------------------------------------------
    for role in ("primary", "monitor"):
        n = sum(1 for b in raw.values() if isinstance(b, dict) and b.get("role") == role)
        if n == 0:
            errors.append(f"no '{role}' agent: exactly one is required")
        elif n > 1:
            errors.append(f"{n} agents have role '{role}': exactly one is allowed")

    seen = {}
    channel_owner = {}
    for a in agents.values():
        for sid, chans in a.shard_defs:
            if not ID_RE.match(sid):
                errors.append(f"shard id '{sid}' (agent '{a.id}') must match {ID_RE.pattern}")
            if sid in agents and sid != a.id:
                errors.append(f"shard id '{sid}' (agent '{a.id}') equals another agent's id")
            if sid in seen:
                errors.append(f"shard id '{sid}' is not unique (agents '{seen[sid]}' and '{a.id}')")
            seen.setdefault(sid, a.id)
            for ch in chans:
                if channel_names is not None and ch not in channel_names:
                    errors.append(f"shard '{sid}': channel '{ch}' is not in channels.json")
                if ch in channel_owner and channel_owner[ch] != sid:
                    errors.append(f"channel '{ch}' belongs to more than one shard "
                                  f"('{channel_owner[ch]}' and '{sid}')")
                channel_owner.setdefault(ch, sid)
    if errors:
        raise RegistryError(errors)
    return Registry(agents, warnings)


def agents_yaml_path(workspace):
    return Path(workspace) / "config" / "agents.yaml"


def load_registry(workspace):
    path = agents_yaml_path(workspace)
    if not path.exists():
        raise RegistryError([f"{path} not found"])
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise RegistryError([f"{path.name} is not valid YAML: {exc}"])
    names, chan_err = _channel_names(workspace)
    reg = parse_registry(data, names)
    if chan_err:
        reg.warnings.append(chan_err)
    return reg


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def write_agent(workspace, agent_id, body):
    """Add agent ``agent_id`` (a schema-shaped dict) to agents.yaml.

    With ruamel.yaml importable the file is edited via a comment-preserving round
    trip; otherwise a new block is appended as text and existing lines are never
    touched. The result is re-validated first and an invalid result is refused
    (RegistryError, file untouched).
    """
    path = agents_yaml_path(workspace)
    names, _ = _channel_names(workspace)
    if not ID_RE.match(agent_id):
        raise RegistryError([f"agent id '{agent_id}' must match {ID_RE.pattern}"])
    original = path.read_text() if path.exists() else f"version: {REGISTRY_VERSION}\nagents:\n"
    try:
        current = yaml.safe_load(original) or {}
    except yaml.YAMLError as exc:
        raise RegistryError([f"{path.name} is not valid YAML: {exc}"])
    if agent_id in (current.get("agents") or {}):
        raise RegistryError([f"agent '{agent_id}' already exists"])

    candidate = {**current, "agents": {**(current.get("agents") or {}), agent_id: body}}
    parse_registry(candidate, names)       # raises on invalid; nothing written yet

    try:
        from ruamel.yaml import YAML       # type: ignore
    except ImportError:
        YAML = None
    if YAML is not None:
        import io
        rt = YAML()
        rt.preserve_quotes = True
        doc = rt.load(original)
        doc["agents"][agent_id] = body
        buf = io.StringIO()
        rt.dump(doc, buf)
        new_text = buf.getvalue()
    else:
        block = yaml.safe_dump({agent_id: body}, sort_keys=False, default_flow_style=False)
        block = "".join("  " + ln if ln.strip() else ln for ln in block.splitlines(True))
        new_text = original + ("" if original.endswith("\n") else "\n") + block
    # Re-validate what will actually land on disk.
    parse_registry(yaml.safe_load(new_text), names)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# legacy migration (called only by lib/migrate/steps/10_registry.py)
# --------------------------------------------------------------------------

LEGACY_BACKUP_SUFFIX = ".pre-2.0"
MONITOR_TEMPLATE = "agents/templates/monitor.md"
# The monitor reads logs and error strings, so it holds no shell: deny rules survive
# --dangerously-skip-permissions, which also keeps AGENT_SERVER_TOKEN out of its reach.
MONITOR_DISALLOWED_TOOLS = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"]
RESERVED_MONITOR_IDS = ("relay", "scheduler", "mcp-tools", "server")   # component names
_DEFAULT_MONITOR = {"name": "relay", "role": "monitor", "model": "haiku",
                    "max_turns": 10, "timeout": 300,
                    "dashboard_chat": False,
                    "disallowed_tools": list(MONITOR_DISALLOWED_TOOLS),
                    "prompt": {"section": MONITOR_TEMPLATE,
                               "core": True, "house_style": True}}


_MCP_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
# Names the spawner already supplies (lib/spawn_env.py allowlist, agent-server identity).
_MCP_ENV_SKIP_PREFIXES = ("KARAKOS_", "AGENT_SERVER_", "LC_", "XDG_")
_MCP_ENV_SKIP = frozenset({"WORKSPACE_ROOT", "PATH", "HOME", "USER", "LANG", "TZ", "TERM",
                           "TMPDIR", "SHELL", "PWD"})


def mcp_env_names(workspace):
    """Env var NAMES the workspace's .mcp.json references as ${NAME} (or
    ${NAME:-default}) in any string: sorted, names only, never values. 2.0 builds
    the claude subprocess env from an allowlist, so a 1.x server that expanded
    ${NAME} from the inherited environment sees nothing unless the agent's
    `env:` names it. Unreadable or absent file: no names."""
    try:
        doc = json.loads((Path(workspace) / ".mcp.json").read_text())
    except (OSError, ValueError):
        return []
    found = set()

    def walk(node):
        if isinstance(node, str):
            found.update(_MCP_REF.findall(node))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return sorted(n for n in found if n not in _MCP_ENV_SKIP
                  and not n.startswith(_MCP_ENV_SKIP_PREFIXES))


def mcp_env_additions(workspace, agents):
    """{agent id: [names]} that migration would add to each agent's `env:`:
    every non-monitor agent in `agents` (id -> entry dict), names not already
    keys of its env. The monitor holds no tools that need them and keeps the
    narrower environment."""
    names = mcp_env_names(workspace)
    out = {}
    for aid, entry in agents.items():
        if entry.get("role") == "monitor":
            continue
        have = entry.get("env") or {}
        add = [n for n in names if n not in have]
        if add:
            out[aid] = add
    return out


def legacy_to_registry_dict(old, channels=None):
    """Convert a parsed 1.x agents.json dict (and channels.json dict) into a
    schema-2 mapping. Pure. Raises RegistryError when the input is unusable."""
    raw = old.get("agents") if isinstance(old, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise RegistryError(["agents.json has no agents to migrate"])
    ids = list(raw)
    roles = {}
    for i, aid in enumerate(ids):
        body = raw[aid] if isinstance(raw[aid], dict) else {}
        prompt = str(body.get("system_prompt") or "")
        if i == 0:
            roles[aid] = "primary"
        elif aid == "relay":
            roles[aid] = "monitor"
        elif ("builder" in aid or "builder" in prompt) and "builder" not in roles.values():
            roles[aid] = "builder"
        elif ("reviewer" in aid or "reviewer" in prompt) and "reviewer" not in roles.values():
            roles[aid] = "reviewer"
        else:
            roles[aid] = "custom"
    agents = {}
    for aid in ids:
        body = raw[aid] if isinstance(raw[aid], dict) else {}
        entry = {"name": aid, "role": roles[aid]}
        for k, v in body.items():
            if k == "discord_bot_token_env":
                entry.setdefault("discord", {})["token_env"] = v
            elif k == "discord_bot_id_env":
                entry.setdefault("discord", {})["bot_id_env"] = v
            elif k == "discord_role_id_env":
                entry.setdefault("discord", {})["role_id_env"] = v
            else:
                entry[k] = v
        # 1.x prompts already carry their own core/house-style text: keep them
        # verbatim (flags off) so nothing is injected twice after the upgrade.
        if "prompt" not in entry:
            entry["prompt"] = {"core": False, "house_style": False}
            if entry.get("system_prompt"):
                entry["prompt"] = {"section": entry["system_prompt"],
                                   **entry["prompt"]}
        agents[aid] = entry
    if "monitor" not in roles.values():
        mid = "relay" if "relay" not in agents else "monitor"
        agents[mid] = {**_DEFAULT_MONITOR, "name": mid,
                       "disallowed_tools": list(MONITOR_DISALLOWED_TOOLS),
                       "prompt": dict(_DEFAULT_MONITOR["prompt"])}
    chans = (channels or {}).get("channels") if isinstance(channels, dict) else None
    if isinstance(chans, dict):
        owned = {}
        for cname, info in chans.items():
            target = info.get("default_agent") if isinstance(info, dict) else None
            if target in agents:
                owned.setdefault(target, []).append(cname)
        for aid, names in owned.items():
            agents[aid]["shards"] = [{"id": aid, "channels": names}]
    return {"version": REGISTRY_VERSION, "agents": agents}


def migrate_legacy(workspace):
    """Convert config/agents.json (+ channels.json default_agent) into
    config/agents.yaml and keep a copy of the original as agents.json.pre-2.0.
    Idempotent: a no-op (returns False) when agents.yaml already exists.
    The original agents.json is left in place."""
    cfg = Path(workspace) / "config"
    legacy, target = cfg / "agents.json", cfg / "agents.yaml"
    if target.exists() or not legacy.exists():
        return False
    try:
        old = json.loads(legacy.read_text())
    except (OSError, ValueError) as exc:
        raise RegistryError([f"config/agents.json unreadable: {exc}"])
    channels = None
    chan_path = cfg / "channels.json"
    if chan_path.exists():
        try:
            channels = json.loads(chan_path.read_text())
        except (OSError, ValueError):
            channels = None
    doc = legacy_to_registry_dict(old, channels)
    for aid, add in mcp_env_additions(workspace, doc["agents"]).items():
        env = dict(doc["agents"][aid].get("env") or {})
        env.update({n: "${%s}" % n for n in add})      # a reference, resolved at spawn
        doc["agents"][aid]["env"] = env
    names, _ = _channel_names(workspace)
    parse_registry(doc, names)                      # refuse before writing anything
    text = ("# Karakos agent registry (schema 2). Migrated from agents.json.\n"
            + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    pre = cfg / ("agents.json" + LEGACY_BACKUP_SUFFIX)
    if not pre.exists():
        pre.write_text(legacy.read_text())
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, target)
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _fmt(val):
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None:
        return ""
    if isinstance(val, (list, dict)):
        return json.dumps(val)
    return str(val)


# --------------------------------------------------------------------------
# init (called by setup.sh on a fresh install)
# --------------------------------------------------------------------------

NAME_MAX = 64


def slugify_id(name):
    """Registry id from a typed name: lowercase letters, digits, hyphens, starting
    with a letter, at most 32 chars; ``karakos`` when nothing usable is left."""
    slug = re.sub(r"[^a-z0-9-]", "-", str(name).lower())
    slug = re.sub(r"^[^a-z]+", "", slug)[:32].strip("-")
    return slug or "karakos"


def clean_display_name(name):
    """Trim; refuse control characters/newlines and names over NAME_MAX."""
    name = str(name).strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise RegistryError(["name must not contain control characters or newlines"])
    if len(name) > NAME_MAX:
        raise RegistryError([f"name must be at most {NAME_MAX} characters"])
    return name


def init_registry(workspace, primary_id, primary_name, monitor_id="monitor", monitor_name=None,
                  monitor_template=MONITOR_TEMPLATE, channels=None,
                  context_budget=150000):
    """Write a fresh config/agents.yaml (primary + monitor). Refuses an existing
    file. The document is built as a dict and dumped by yaml.safe_dump, so no name
    can break the YAML."""
    path = agents_yaml_path(workspace)
    if path.exists():
        raise RegistryError([f"{path} already exists; refusing to overwrite"])
    primary_name = clean_display_name(primary_name) or primary_id
    monitor_name = clean_display_name(monitor_name) if monitor_name else monitor_id
    if monitor_id in RESERVED_MONITOR_IDS:
        raise RegistryError([f"monitor id '{monitor_id}' is reserved (it names a system "
                             f"component); choose another"])
    if primary_id == monitor_id:
        raise RegistryError([f"primary id '{primary_id}' equals the monitor id; choose another name"])
    doc = {
        "version": REGISTRY_VERSION,
        "agents": {
            primary_id: {
                "name": primary_name,
                "role": "primary",
                "model": "sonnet",
                "max_turns": 200,
                "timeout": 10800,
                "prompt": {"section": f"agents/{primary_id}/SYSTEM_PROMPT.md",
                           "core": True, "house_style": True},
                "context_budget_tokens": int(context_budget),
                "tool_streaming": True,
                "stream_to_channel": True,
                "discord": {"token_env": "DISCORD_BOT_TOKEN_PRIMARY",
                            "bot_id_env": "DISCORD_BOT_ID_PRIMARY"},
                "shards": [{"id": primary_id, "channels": list(channels or ["general"])}],
            },
            monitor_id: {
                "name": monitor_name,
                "role": "monitor",
                "model": "haiku",
                "max_turns": 10,
                "timeout": 300,
                "prompt": {"section": monitor_template, "core": True, "house_style": True},
                "tool_streaming": False,
                "stream_to_channel": False,
                "dashboard_chat": False,
                "disallowed_tools": list(MONITOR_DISALLOWED_TOOLS),
            },
        },
    }
    parse_registry(doc, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False,
                                  allow_unicode=True))
    os.replace(tmp, path)


def _init_main(ws, args):
    import argparse
    ap = argparse.ArgumentParser(prog="registry.py init")
    ap.add_argument("--workspace", default=ws)
    ap.add_argument("--primary-id", default=None)
    ap.add_argument("--primary-name", required=True)
    ap.add_argument("--monitor-id", default="monitor")
    ap.add_argument("--monitor-name", default=None)
    ap.add_argument("--monitor-template", default=MONITOR_TEMPLATE)
    ap.add_argument("--channel", action="append", default=None)
    ap.add_argument("--context-budget", type=int, default=150000)
    a = ap.parse_args(args)
    try:
        pid = a.primary_id or slugify_id(a.primary_name)
        init_registry(a.workspace, pid, a.primary_name, a.monitor_id, a.monitor_name,
                      a.monitor_template, a.channel, a.context_budget)
    except RegistryError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ws = os.environ.get("WORKSPACE_ROOT") or str(Path(__file__).resolve().parent.parent)
    if argv[:1] == ["--workspace"] and len(argv) >= 2:
        ws, argv = argv[1], argv[2:]
    usage = "usage: registry.py [--workspace DIR] ids | field <id> <key> | role <role> | legacy | validate"
    if not argv:
        print(usage, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "init":
        return _init_main(ws, args)
    try:
        reg = load_registry(ws)
    except RegistryError as exc:
        print(exc, file=sys.stderr)
        return 1
    for w in reg.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if cmd == "validate" and not args:
        print("ok")
        return 0
    if cmd == "legacy" and not args:
        print(json.dumps(reg.legacy_view()))
        return 0
    if cmd == "ids" and not args:
        print("\n".join(reg.ids()))
        return 0
    if cmd == "role" and len(args) == 1:
        if args[0] not in ROLES:
            print(f"unknown role '{args[0]}'", file=sys.stderr)
            return 2
        print("\n".join(a.id for a in reg.by_role(args[0])))
        return 0
    if cmd == "field" and len(args) == 2:
        aid, key = args
        if aid not in reg.ids():
            print(f"unknown agent '{aid}'", file=sys.stderr)
            return 1
        a = reg.agent(aid)
        if key not in _KNOWN_KEYS:
            print(f"unknown field '{key}'", file=sys.stderr)
            return 1
        if key == "shards":
            val = [{"id": s.id, "channels": list(s.channels)} for s in reg.shards_of(aid)]
        else:
            val = a.get(key)
        print(_fmt(val))
        return 0
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
