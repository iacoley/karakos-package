"""
Tests for issue #110: docs/EXTENDING.md must not teach the "Agent Skills"
name as if it were Claude Code's native frontmatter-SKILL.md system, and
the repo must ship a root CLAUDE.md so an agent running inside an install
has project instructions of its own.

These are docs-content assertions, not behavioral ones — the runtime fix
for skill discovery itself is covered by tests/test_tools_server.py (#83, #84).
"""

from conftest import PACKAGE_ROOT


def test_claude_md_exists_at_repo_root():
    claude_md = PACKAGE_ROOT / "CLAUDE.md"
    assert claude_md.exists(), (
        "repo ships no CLAUDE.md, so an agent running inside an install "
        "has no project instructions of its own (issue #110)"
    )


def test_claude_md_documents_the_skill_convention():
    content = (PACKAGE_ROOT / "CLAUDE.md").read_text()
    assert "tools.json" in content
    assert "scripts" in content
    # Must point at the fuller guide rather than duplicating it.
    assert "docs/EXTENDING.md" in content


def test_claude_md_does_not_present_frontmatter_as_a_valid_option():
    """The convention decision (#83/#84: tools.json + scripts/) must be
    documented as settled, not offered as a choice alongside frontmatter
    SKILL.md."""
    content = (PACKAGE_ROOT / "CLAUDE.md").read_text()
    assert "not" in content.lower()
    assert "will not be picked up" in content or "will not load" in content


def test_extending_md_distinguishes_from_claude_code_agent_skills():
    """Issue #110: EXTENDING.md documented the tools.json flow under the
    'Skills' name without telling the reader it differs from Claude Code's
    own Agent Skills (frontmatter SKILL.md) convention."""
    content = (PACKAGE_ROOT / "docs" / "EXTENDING.md").read_text()
    skill_section = content[content.index("## Adding a Skill"):]

    assert "Agent Skills" in skill_section
    assert "tools.json" in skill_section
    assert "scripts/" in skill_section
    # It must say plainly that a frontmatter-only SKILL.md will not work
    # here, not present the two conventions as an open choice.
    assert "will not load" in skill_section


def test_extending_md_skill_section_precedes_walkthrough():
    """The clarifying note must land before the reader starts following
    the steps, not buried after."""
    content = (PACKAGE_ROOT / "docs" / "EXTENDING.md").read_text()
    heading_idx = content.index("## Adding a Skill")
    note_idx = content.index("Agent Skills")
    steps_idx = content.index("### 1. Create the Skill Directory")
    assert heading_idx < note_idx < steps_idx
