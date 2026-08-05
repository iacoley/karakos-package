"""
Tests for load_server_ids() in bin/relay.py — which Discord servers the relay
accepts messages from.

The single-server `server_id` written by setup.sh has to keep working
untouched; `server_ids` is the additive path for installs that also join a
shared server.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    """Import bin/relay.py with WORKSPACE_ROOT pointed at a temp tree.

    Import-time side effects (the rotating log handler) need logs/ to exist.
    """
    workspace = tmp_path_factory.mktemp("workspace")
    (workspace / "logs").mkdir()

    import os
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("relay_under_test", RELAY_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["relay_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return module


def test_single_server_id_still_works(relay):
    assert relay.load_server_ids({"server_id": "111"}) == {"111"}


def test_server_ids_list_is_accepted(relay):
    assert relay.load_server_ids({"server_ids": ["222", "333"]}) == {"222", "333"}


def test_both_keys_are_unioned(relay):
    got = relay.load_server_ids({"server_id": "111", "server_ids": ["222"]})
    assert got == {"111", "222"}


def test_ints_are_normalized_to_strings(relay):
    """message.guild.id is compared as a string, so the config must normalize."""
    got = relay.load_server_ids({"server_id": 111, "server_ids": [222]})
    assert got == {"111", "222"}


def test_bare_string_server_ids_is_treated_as_one_id(relay):
    """A hand-edited config is as likely to write a string as a list."""
    assert relay.load_server_ids({"server_ids": "222"}) == {"222"}


def test_empty_config_accepts_nothing(relay):
    """No servers configured must not mean 'accept every server'."""
    assert relay.load_server_ids({}) == set()
    assert relay.load_server_ids({"server_id": "", "server_ids": []}) == set()
