"""Unattended setup: lib/setup_answers.py parser and validation, and the wiring
in setup.sh / install.sh / install.ps1. No Docker, no network."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import setup_answers as sa  # noqa: E402

GOOD = {
    "system_name": "Hearth", "owner_name": "Alex",
    "discord_bot_token": "tok", "discord_bot_id": "1000000000000001",
    "discord_server_id": "1000000000000002", "channel_general": "1000000000000003",
    "channel_signals": "1000000000000004", "owner_discord_id": "1000000000000005",
}


def write(tmp_path, data):
    p = tmp_path / "answers.json"
    p.write_text(json.dumps(data))
    return p


def test_minimal_file_gets_wizard_defaults(tmp_path):
    a, errs = sa.load(write(tmp_path, GOOD), env={})
    assert errs == []
    assert a["primary_agent_name"] == "Hearth"       # defaults to the system name
    assert a["monitor_agent_name"] == "monitor"
    assert (a["cost_daily_limit"], a["cost_monthly_limit"]) == ("25.00", "500.00")
    assert a["channel_staff"] == ""


def test_missing_required_fields_are_all_listed(tmp_path):
    a, errs = sa.load(write(tmp_path, {"system_name": "x"}), env={})
    assert a == {}
    for f in ("owner_name", "discord_bot_token", "discord_bot_id", "discord_server_id",
              "channel_general", "channel_signals", "owner_discord_id"):
        assert any(f"'{f}'" in e for e in errs), f
    assert not any("'system_name'" in e for e in errs)


def test_secret_from_env_var(tmp_path):
    data = {k: v for k, v in GOOD.items() if k != "discord_bot_token"}
    data["discord_bot_token_env"] = "MY_BOT"
    a, errs = sa.load(write(tmp_path, data), env={"MY_BOT": "s3cret"})
    assert errs == [] and a["discord_bot_token"] == "s3cret"


def test_unset_env_var_is_an_error_naming_the_variable(tmp_path):
    data = {k: v for k, v in GOOD.items() if k != "discord_bot_token"}
    data["discord_bot_token_env"] = "MY_BOT"
    _, errs = sa.load(write(tmp_path, data), env={})
    assert any("MY_BOT" in e and "not set" in e for e in errs)


def test_literal_wins_over_env_reference(tmp_path):
    data = dict(GOOD, discord_bot_token_env="MY_BOT")
    a, errs = sa.load(write(tmp_path, data), env={"MY_BOT": "other"})
    assert errs == [] and a["discord_bot_token"] == "tok"


def test_bad_env_var_name_rejected(tmp_path):
    data = dict(GOOD, claude_oauth_token_env="not a name; rm")
    _, errs = sa.load(write(tmp_path, data), env={})
    assert any("environment variable name" in e for e in errs)


def test_unknown_key_is_an_error_but_underscore_keys_are_comments(tmp_path):
    _, errs = sa.load(write(tmp_path, dict(GOOD, sytem_name="typo")), env={})
    assert any("unknown field 'sytem_name'" in e for e in errs)
    _, errs = sa.load(write(tmp_path, dict(GOOD, _comment="hi")), env={})
    assert errs == []


@pytest.mark.parametrize("field", sa.SNOWFLAKES[:1] + ("owner_discord_id", "channel_general"))
def test_discord_ids_must_be_numeric(tmp_path, field):
    _, errs = sa.load(write(tmp_path, dict(GOOD, **{field: "not-an-id"})), env={})
    assert any(f"'{field}'" in e for e in errs)


def test_numbers_accepted_as_json_numbers(tmp_path):
    a, errs = sa.load(write(tmp_path, dict(GOOD, cost_daily_limit=10, cost_monthly_limit=100.5)), env={})
    assert errs == [] and a["cost_daily_limit"] == "10"


@pytest.mark.parametrize("daily,monthly", [("0", "500"), ("abc", "500"), ("-1", "500"), ("600", "500")])
def test_cost_limits_validated(tmp_path, daily, monthly):
    _, errs = sa.load(write(tmp_path, dict(GOOD, cost_daily_limit=daily, cost_monthly_limit=monthly)), env={})
    assert errs


def test_newline_in_value_rejected_because_it_lands_in_dot_env(tmp_path):
    _, errs = sa.load(write(tmp_path, dict(GOOD, owner_name="Alex\nEVIL=1")), env={})
    assert any("control character" in e for e in errs)


@pytest.mark.parametrize("monitor", ["relay", "Scheduler", "mcp-tools", "server", "Hearth"])
def test_monitor_name_rules_match_the_wizard(tmp_path, monitor):
    _, errs = sa.load(write(tmp_path, dict(GOOD, monitor_agent_name=monitor)), env={})
    assert any("monitor_agent_name" in e for e in errs)


def test_monitor_colliding_with_explicit_primary(tmp_path):
    _, errs = sa.load(write(tmp_path, dict(GOOD, primary_agent_name="Watcher", monitor_agent_name="watcher")), env={})
    assert errs


def test_unreadable_and_non_object_files(tmp_path):
    assert sa.load(tmp_path / "nope.json", env={})[1]
    p = tmp_path / "x.json"
    p.write_text("{not json")
    assert "cannot read" in sa.load(p, env={})[1][0]
    p.write_text("[]")
    assert "JSON object" in sa.load(p, env={})[1][0]


def test_cli_prints_json_on_success_and_problem_list_on_failure(tmp_path):
    ok = subprocess.run([sys.executable, str(ROOT / "lib/setup_answers.py"), str(write(tmp_path, GOOD))],
                        capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)["system_name"] == "Hearth"
    bad = subprocess.run([sys.executable, str(ROOT / "lib/setup_answers.py"),
                          str(write(tmp_path, {}))], capture_output=True, text=True)
    assert bad.returncode == 2 and "missing required field 'system_name'" in bad.stderr
    assert bad.stdout == ""


def test_example_file_parses_once_its_env_var_is_set():
    a, errs = sa.load(ROOT / "docs/answers.example.json", env={"KARAKOS_DISCORD_BOT_TOKEN": "t"})
    assert errs == [], errs
    assert set(a) == set(sa.FIELDS)


def test_every_wizard_field_is_in_the_answers_schema():
    """The wizard's state keys (setup.sh save_state) are exactly the answers fields."""
    keys = set(re.findall(r"save_state (\w+) ", (ROOT / "setup.sh").read_text()))
    assert keys <= set(sa.FIELDS), keys - set(sa.FIELDS)
    assert set(sa.FIELDS) - keys == {"claude_oauth_token"}


# ---- wiring ---------------------------------------------------------------

def test_setup_sh_accepts_answers_and_never_prompts_in_that_mode():
    s = (ROOT / "setup.sh").read_text()
    assert "--answers" in s and "KARAKOS_ANSWERS" in s
    for marker in ("Resume from previous setup", "Continue anyway?", "Ready to launch"):
        i = s.index(marker)
        assert "ANSWERS_FILE" in s[max(0, i - 200):i], f"prompt not gated: {marker}"
    r = subprocess.run(["bash", str(ROOT / "setup.sh"), "--answers"], capture_output=True, text=True)
    assert r.returncode == 2 and "needs a file" in r.stderr


def test_install_sh_pins_main_and_passes_answers_through():
    s = (ROOT / "install.sh").read_text()
    assert 'KARAKOS_BRANCH="${KARAKOS_BRANCH:-main}"' in s
    clones = re.findall(r"^\s*git clone .*$", s, re.M)
    assert clones and all('--branch "$KARAKOS_BRANCH"' in c for c in clones), clones
    assert "--answers" in s and "KARAKOS_ANSWERS" in s
    assert 'SETUP_ARGS+=(--answers "$ANSWERS_FILE")' in s
    assert './setup.sh ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}' in s


def test_install_ps1_pins_main():
    s = (ROOT / "install.ps1").read_text()
    assert '"main"' in s and "KARAKOS_BRANCH" in s
    clones = re.findall(r"^\s*git clone .*$", s, re.M)
    assert clones and all("--branch $KarakosBranch" in c for c in clones), clones


def test_install_sh_rejects_missing_answers_file():
    r = subprocess.run(["bash", str(ROOT / "install.sh"), "--answers", "/nonexistent.json"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "answers file not found" in r.stderr
