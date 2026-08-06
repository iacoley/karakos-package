"""
Tests for bin/register-discord-commands.py — issue #87.

The bot needs a `/` command list that (a) actually contains names and
descriptions Discord can render, and (b) talks correctly to the Discord
application-commands REST endpoint, including the one failure mode the
issue calls out by name: a 403 when the bot was invited without the
`applications.commands` scope.

Real Discord is never contacted. `KARAKOS_DISCORD_API_BASE` (read by the
script) is pointed at a throwaway local HTTP server that stands in for
`discord.com/api/v10`, so PUT/GET semantics are exercised for real without
any network access or a live guild.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
SCRIPT = PACKAGE_ROOT / "bin" / "register-discord-commands.py"


class FakeDiscordHandler(BaseHTTPRequestHandler):
    """Stands in for the Discord applications/.../commands endpoint."""

    # Set per-test on the class before the server is started.
    response_status = 200

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else None

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        payload = self._read_body()
        self.server.last_payload = payload
        self.server.last_path = self.path
        if self.response_status == 200:
            self._reply(200, payload)
        else:
            self._reply(self.response_status, {"message": "Missing Access", "code": 50001})

    def do_GET(self):
        self.server.last_path = self.path
        if self.response_status == 200:
            self._reply(200, self.server.list_response)
        else:
            self._reply(self.response_status, {"message": "Missing Access", "code": 50001})

    def log_message(self, *args):
        pass  # keep test output quiet


@pytest.fixture
def fake_discord():
    """Start a fake Discord API server and yield (base_url, server)."""
    FakeDiscordHandler.response_status = 200
    server = HTTPServer(("127.0.0.1", 0), FakeDiscordHandler)
    server.last_payload = None
    server.last_path = None
    server.list_response = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def run_script(args, env_overrides, base_url=None):
    env = {**os.environ, **env_overrides}
    if base_url is not None:
        env["KARAKOS_DISCORD_API_BASE"] = base_url
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


VALID_ENV = {
    "DISCORD_BOT_TOKEN_PRIMARY": "test-token",
    "DISCORD_BOT_ID_PRIMARY": "111222333",
    "DISCORD_SERVER_ID": "444555666",
}


def test_every_command_has_a_name_and_a_description():
    """This is the actual acceptance test for #87: typing `/` in Discord
    must show commands with descriptions. Discord can only render what we
    hand it, so the command table itself is the thing to check."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("register_discord_commands", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.COMMANDS, "no commands defined to register"
    for cmd in module.COMMANDS:
        assert cmd.get("name"), f"command missing a name: {cmd}"
        assert cmd.get("description", "").strip(), f"/{cmd.get('name')} has no description"
        for opt in cmd.get("options", []):
            assert opt.get("description", "").strip(), (
                f"/{cmd['name']} option {opt.get('name')} has no description"
            )


def test_register_success_puts_the_full_command_list(fake_discord):
    base_url, server = fake_discord

    result = run_script([], VALID_ENV, base_url)

    assert result.returncode == 0, result.stderr
    assert "Registered" in result.stdout
    assert server.last_path == "/applications/111222333/guilds/444555666/commands"
    assert server.last_payload, "no commands were PUT to the fake endpoint"
    names = {c["name"] for c in server.last_payload}
    assert "health" in names
    assert "help" in names


def test_403_reports_the_scope_fix_not_a_bare_traceback(fake_discord):
    base_url, server = fake_discord
    FakeDiscordHandler.response_status = 403

    result = run_script([], VALID_ENV, base_url)

    assert result.returncode == 1
    assert "403" in result.stderr
    assert "applications.commands" in result.stderr
    assert "Manage Server" not in result.stderr  # that instruction lives in the docs, not here


def test_clear_sends_an_empty_command_list(fake_discord):
    base_url, server = fake_discord

    result = run_script(["--clear"], VALID_ENV, base_url)

    assert result.returncode == 0
    assert server.last_payload == []


def test_list_prints_registered_commands_with_subcommand_options(fake_discord):
    base_url, server = fake_discord
    server.list_response = [
        {"name": "cost", "options": []},
        {"name": "agent", "options": [{"name": "name", "type": 3}]},
    ]

    result = run_script(["--list"], VALID_ENV, base_url)

    assert result.returncode == 0
    assert "/cost" in result.stdout
    assert "/agent" in result.stdout


@pytest.mark.parametrize("missing", ["DISCORD_BOT_TOKEN_PRIMARY", "DISCORD_BOT_ID_PRIMARY", "DISCORD_SERVER_ID"])
def test_missing_env_var_fails_loudly_instead_of_a_bad_request(missing, fake_discord):
    base_url, _server = fake_discord
    env = {k: v for k, v in VALID_ENV.items() if k != missing}

    result = run_script([], env, base_url)

    assert result.returncode != 0
    assert missing in result.stderr
