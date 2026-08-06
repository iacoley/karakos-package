"""
Tests for .github/ISSUE_TEMPLATE/ — issue #109.

The template must present a required "Install-visible acceptance test" field,
phrased as user action -> observable outcome, so an issue can't be filed
without one.
"""

from pathlib import Path

import yaml

from conftest import PACKAGE_ROOT

ISSUE_TEMPLATE_DIR = PACKAGE_ROOT / ".github" / "ISSUE_TEMPLATE"


def _load_templates():
    return [
        yaml.safe_load(p.read_text())
        for p in ISSUE_TEMPLATE_DIR.glob("*.yml")
        if p.name != "config.yml"
    ]


def test_issue_template_dir_exists():
    assert ISSUE_TEMPLATE_DIR.is_dir()


def test_blank_issues_disabled():
    """Blank issues must be off, or the required field is trivially bypassed."""
    config = yaml.safe_load((ISSUE_TEMPLATE_DIR / "config.yml").read_text())
    assert config["blank_issues_enabled"] is False


def test_has_required_acceptance_test_field():
    templates = _load_templates()
    assert templates, "expected at least one issue form template"

    matches = []
    for template in templates:
        for field in template.get("body", []):
            label = field.get("attributes", {}).get("label", "")
            if "install-visible acceptance test" in label.lower():
                matches.append(field)

    assert matches, "no field labeled 'Install-visible acceptance test' found"
    for field in matches:
        assert field.get("validations", {}).get("required") is True


def test_acceptance_test_field_phrasing_guidance():
    """The field must instruct: user action -> observable outcome."""
    templates = _load_templates()
    found = False
    for template in templates:
        for field in template.get("body", []):
            label = field.get("attributes", {}).get("label", "")
            if "install-visible acceptance test" not in label.lower():
                continue
            found = True
            description = field["attributes"].get("description", "").lower()
            placeholder = field["attributes"].get("placeholder", "").lower()
            text = description + placeholder
            assert "action" in text
            assert "outcome" in text or "pass =" in text
    assert found
