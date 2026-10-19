"""Runtime overrides (spec 6.4): the agent-level `effort` set from a command.

`data/runtime-overrides.json`: `{"agents": {"<agent id>": {"effort": "<level>"}}}`.
A new file the server creates on first use; it holds no 1.x data and the
registry file is never edited at runtime. Written atomically, read tolerantly
(missing or corrupt means no overrides), read at every spawn.

The same file holds the agent-level `model` (issue 233). The allowed model ids
are listed in `config/models.yaml` (`allowed: [...]`), never in code. With no
readable list, only the agent's own registry model is allowed.

Effective effort: override, else the registry `effort`, else None (the CLI's
own default; the flag is omitted).
"""
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from registry import EFFORTS

_log = logging.getLogger("runtime_overrides")
DEFAULT = "default"

_cli_has_effort: Optional[bool] = None


def path_for(workspace) -> Path:
    return Path(workspace) / "data" / "runtime-overrides.json"


def load(workspace) -> dict:
    try:
        raw = json.loads(path_for(workspace).read_text())
        agents = raw["agents"]
        if not isinstance(agents, dict):
            raise ValueError("agents is not an object")
        return {k: dict(v) for k, v in agents.items() if isinstance(v, dict)}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError) as e:
        _log.warning(f"runtime overrides unreadable ({e}); ignoring")
        return {}


def _save(workspace, agents: dict) -> None:
    p = path_for(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"agents": agents}, indent=1))
    os.replace(tmp, p)


def set_effort(workspace, agent: str, level: str) -> Optional[str]:
    """Record the override (`default` removes it). Returns the stored level or
    None. Raises ValueError for an unknown level."""
    if level != DEFAULT and level not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}, {DEFAULT}")
    agents = load(workspace)
    if level == DEFAULT:
        entry = agents.get(agent, {})
        entry.pop("effort", None)
        if entry:
            agents[agent] = entry
        else:
            agents.pop(agent, None)
        _save(workspace, agents)
        return None
    agents.setdefault(agent, {})["effort"] = level
    _save(workspace, agents)
    return level


def effective_effort(workspace, agent: str, config: Optional[dict]) -> Tuple[Optional[str], str]:
    """(level or None, source: override | registry | default)."""
    level = (load(workspace).get(agent) or {}).get("effort")
    if level in EFFORTS:
        return level, "override"
    level = (config or {}).get("effort")
    if level in EFFORTS:
        return level, "registry"
    return None, "default"


def cli_supports_effort() -> bool:
    """Does `claude --help` list `--effort`? Probed once. Anything unreadable
    counts as supported: only a clear help text without the flag says no."""
    global _cli_has_effort
    if _cli_has_effort is None:
        try:
            out = subprocess.run(["claude", "--help"], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=5).stdout
            _cli_has_effort = ("--effort" in out) if "--model" in out else True
        except Exception:  # noqa: BLE001
            _cli_has_effort = True
    return _cli_has_effort


MODELS_CONFIG = ("config", "models.yaml")


def allowed_models(workspace, config: Optional[dict] = None) -> list:
    """Model ids a runtime switch may use: `allowed` in config/models.yaml.
    Missing or unreadable: just the agent's registry model, if it has one."""
    try:
        import yaml
        raw = yaml.safe_load(Path(workspace).joinpath(*MODELS_CONFIG).read_text()) or {}
        ids = raw.get("allowed")
        if isinstance(ids, list):
            out = [m for m in ids if isinstance(m, str) and m]
            if out:
                return out
    except Exception as e:  # noqa: BLE001 - absent file is the normal case
        if not isinstance(e, FileNotFoundError):
            _log.warning(f"models config unreadable ({e}); using the registry model only")
    m = (config or {}).get("model")
    return [m] if isinstance(m, str) and m else []


def set_model(workspace, agent: str, model: str, config: Optional[dict] = None) -> Optional[str]:
    """Record the model override (`default` removes it). Returns the stored id or
    None. Raises ValueError for an id outside the allowed list."""
    agents = load(workspace)
    if model == DEFAULT:
        entry = agents.get(agent, {})
        entry.pop("model", None)
        if entry:
            agents[agent] = entry
        else:
            agents.pop(agent, None)
        _save(workspace, agents)
        return None
    allowed = allowed_models(workspace, config)
    if model not in allowed:
        raise ValueError(f"model must be one of {', '.join(allowed + [DEFAULT])}")
    agents.setdefault(agent, {})["model"] = model
    _save(workspace, agents)
    return model


def effective_model(workspace, agent: str, config: Optional[dict]) -> Tuple[str, str]:
    """(model, source: override | registry | default). An override that has left
    the allowed list since it was set is ignored."""
    m = (load(workspace).get(agent) or {}).get("model")
    if isinstance(m, str) and m in allowed_models(workspace, config):
        return m, "override"
    m = (config or {}).get("model")
    if isinstance(m, str) and m:
        return m, "registry"
    return "sonnet", "default"
