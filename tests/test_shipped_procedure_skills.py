"""The procedure skills the package ships (#235 build B) validate and say what they should."""
import pytest

from conftest import PACKAGE_ROOT
from lib import procedure_skills as ps

SHIPPED = ["restart-and-recover", "diag", "estimate", "worktree-setup", "skill-author"]
SKILLS = PACKAGE_ROOT / "skills"


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_procedure_skill_validates(name):
    assert not (SKILLS / name / "tools.json").exists()
    result = ps.validate(SKILLS, name)
    assert result["ok"] and result["kind"] == "procedure", result["problems"]
    assert ps.read(SKILLS, name)["content"].strip()


def test_list_includes_every_shipped_skill_with_a_description():
    listed = {s["name"]: s for s in ps.scan(SKILLS)}
    for name in SHIPPED:
        assert listed[name]["kind"] == "procedure" and listed[name]["description"]


def test_diag_names_only_real_sections():
    text = (SKILLS / "diag" / "SKILL.md").read_text()
    script = (PACKAGE_ROOT / "bin" / "self-diagnose.py").read_text()
    for section in ("health", "findings", "heartbeats", "hook", "disk", "schema", "logs"):
        assert f'"{section}"' in script and section in text
    assert "bin/self-diagnose.py" in text and "bin/self-test.py" in text
    assert (PACKAGE_ROOT / "bin" / "self-test.py").exists()


def test_skills_reference_admin_tools_that_exist():
    import re
    admin = (PACKAGE_ROOT / "mcp" / "admin-server.py").read_text()
    tools_server = (PACKAGE_ROOT / "mcp" / "tools-server.py").read_text()
    assert '"tail_log"' in admin and '"ask_user"' in tools_server
    for name in SHIPPED:
        text = (SKILLS / name / "SKILL.md").read_text()
        for tool in re.findall(r"`((?:get|set|list|reload|reset|pause|resume|tail)_[a-z_]+)`", text):
            assert f'"{tool}"' in admin, f"{name} mentions admin tool {tool} that does not exist"


def test_restart_skill_asks_before_reset_and_picks_one_reset():
    text = (SKILLS / "restart-and-recover" / "SKILL.md").read_text()
    assert "Ask the owner first" in text and "ask_user" in text
    assert "do not also call `reset_agent`" in text
