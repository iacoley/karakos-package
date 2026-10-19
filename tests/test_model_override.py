"""/model (#233): override file, allowlist from config, effective model, wiring."""
import sys

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import runtime_overrides as ro  # noqa: E402


def models(ws, ids):
    (ws / "config").mkdir(exist_ok=True)
    (ws / "config" / "models.yaml").write_text("allowed:\n" + "".join(f"  - {i}\n" for i in ids))


def test_allowed_comes_from_config_else_registry_model(tmp_path):
    assert ro.allowed_models(tmp_path, {"model": "sonnet"}) == ["sonnet"]
    assert ro.allowed_models(tmp_path, {}) == []
    models(tmp_path, ["opus", "claude-x-1"])
    assert ro.allowed_models(tmp_path, {"model": "sonnet"}) == ["opus", "claude-x-1"]
    (tmp_path / "config" / "models.yaml").write_text("allowed: [[")  # unreadable
    assert ro.allowed_models(tmp_path, {"model": "haiku"}) == ["haiku"]


def test_set_and_effective_order(tmp_path):
    models(tmp_path, ["opus", "haiku"])
    cfg = {"model": "sonnet"}
    assert ro.effective_model(tmp_path, "a", cfg) == ("sonnet", "registry")
    assert ro.effective_model(tmp_path, "a", {}) == ("sonnet", "default")
    assert ro.set_model(tmp_path, "a", "opus", cfg) == "opus"
    assert ro.effective_model(tmp_path, "a", cfg) == ("opus", "override")
    assert ro.effective_model(tmp_path, "b", cfg) == ("sonnet", "registry")
    assert ro.set_model(tmp_path, "a", "default", cfg) is None
    assert ro.effective_model(tmp_path, "a", cfg) == ("sonnet", "registry")


def test_unlisted_model_is_refused_and_nothing_is_written(tmp_path):
    models(tmp_path, ["opus"])
    with pytest.raises(ValueError, match="opus"):
        ro.set_model(tmp_path, "a", "gpt-9", {"model": "sonnet"})
    assert ro.load(tmp_path) == {}


def test_override_that_left_the_list_is_ignored(tmp_path):
    models(tmp_path, ["opus"])
    ro.set_model(tmp_path, "a", "opus", {})
    models(tmp_path, ["haiku"])
    assert ro.effective_model(tmp_path, "a", {"model": "sonnet"}) == ("sonnet", "registry")


def test_model_and_effort_overrides_coexist(tmp_path):
    models(tmp_path, ["opus"])
    ro.set_effort(tmp_path, "a", "high")
    ro.set_model(tmp_path, "a", "opus", {})
    assert ro.load(tmp_path)["a"] == {"effort": "high", "model": "opus"}
    ro.set_model(tmp_path, "a", "default", {})
    assert ro.load(tmp_path)["a"] == {"effort": "high"}
    ro.set_effort(tmp_path, "a", "default")
    assert ro.load(tmp_path) == {}


def test_shipped_models_config_parses_and_has_aliases():
    ids = ro.allowed_models(PACKAGE_ROOT, {})
    assert {"opus", "sonnet", "haiku"} <= set(ids)


def test_wiring_present():
    srv = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    assert 'add_post("/agents/{name}/model"' in srv
    assert 'effective_model(WORKSPACE_ROOT, agent, config)[0]' in srv
    relay = (PACKAGE_ROOT / "bin" / "relay.py").read_text()
    assert '"effort", "model"' in relay and "async def sys_model" in relay
