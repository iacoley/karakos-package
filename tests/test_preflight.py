"""
Tests for bin/preflight.sh

Strategy:
  - Point PREFLIGHT_REPO_ROOT at a tmp directory so each test controls
    config/.env without touching the real repo.
  - Prepend a mock_bin directory to PATH to stub out docker, uname, file,
    ss, df, and other system commands.
  - Run the script with subprocess and assert exit code / stdout.

Slow tests (those that intentionally trigger the 10-second timeout) are marked
@pytest.mark.slow and excluded from the default pytest run via:
    pytest tests/ -m "not slow"
"""

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
PREFLIGHT = PACKAGE_ROOT / "bin" / "preflight.sh"

# Minimal valid .env content for tests that don't care about env-var specifics.
VALID_ENV = textwrap.dedent("""\
    DASHBOARD_PORT=3000
    AGENT_SERVER_TOKEN=test-token
    DISCORD_BOT_TOKEN_PRIMARY=test-discord-token
""")


# =============================================================================
# Helpers
# =============================================================================

def write_mock(bin_dir: Path, name: str, content: str) -> Path:
    """Write an executable mock script."""
    p = bin_dir / name
    p.write_text(content)
    p.chmod(0o755)
    return p


def make_standard_docker(bin_dir: Path, *, compose_version: str = "v2.27.0",
                          docker_root: str = "/var/lib/docker") -> None:
    """Write a mock docker that passes all checks."""
    write_mock(bin_dir, "docker", f"""\
#!/bin/bash
if [ "$1" = "info" ]; then
    echo "Docker Root Dir: {docker_root}"
    exit 0
elif [ "$1" = "compose" ] && [ "$2" = "version" ]; then
    echo "Docker Compose version {compose_version}"
    exit 0
fi
exit 0
""")


def make_standard_df(bin_dir: Path, free_gb: int = 50) -> None:
    """Write a mock df that reports the given free GB."""
    write_mock(bin_dir, "df", f"""\
#!/bin/bash
echo "Filesystem      1G-blocks  Used Available Use% Mounted on"
echo "/dev/sda1             100    50      {free_gb}G  50% /"
""")


def make_standard_ss(bin_dir: Path, *, port_in_use: int | None = None) -> None:
    """Write a mock ss. If port_in_use is set, that port appears as listening."""
    if port_in_use is not None:
        write_mock(bin_dir, "ss", f"""\
#!/bin/bash
echo "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port"
echo "tcp   LISTEN 0      128   0.0.0.0:{port_in_use} 0.0.0.0:*"
""")
    else:
        write_mock(bin_dir, "ss", """\
#!/bin/bash
echo "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port"
""")


def make_standard_file(bin_dir: Path, *, crlf_file: str | None = None) -> None:
    """Write a mock 'file' command. If crlf_file is given, that basename gets CRLF output."""
    write_mock(bin_dir, "file", f"""\
#!/bin/bash
base="$(basename "$1")"
if [ "$base" = "{crlf_file or '__no_crlf__'}" ]; then
    echo "$1: Bourne-Again shell script, ASCII text executable, with CRLF line terminators"
else
    echo "$1: Bourne-Again shell script, ASCII text executable"
fi
""")


def make_full_mock_bin(tmp_path: Path, **kwargs) -> Path:
    """
    Create a mock_bin directory pre-populated with passing stubs.

    Keyword overrides:
      compose_version (str)   — docker compose version string
      docker_root (str)       — Docker Root Dir reported by docker info
      disk_free_gb (int)      — free GB reported by df
      port_in_use (int|None)  — port reported as in use by ss
      crlf_file (str|None)    — basename of file to report as CRLF
    """
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir(exist_ok=True)

    compose_version = kwargs.get("compose_version", "v2.27.0")
    docker_root = kwargs.get("docker_root", "/var/lib/docker")
    disk_free_gb = kwargs.get("disk_free_gb", 50)
    port_in_use = kwargs.get("port_in_use", None)
    crlf_file = kwargs.get("crlf_file", None)

    make_standard_docker(bin_dir, compose_version=compose_version, docker_root=docker_root)
    make_standard_df(bin_dir, free_gb=disk_free_gb)
    make_standard_ss(bin_dir, port_in_use=port_in_use)
    make_standard_file(bin_dir, crlf_file=crlf_file)

    # uname always returns x86_64 unless overridden
    write_mock(bin_dir, "uname", """\
#!/bin/bash
if [ "$1" = "-m" ]; then echo "x86_64"; else /usr/bin/uname "$@"; fi
""")

    return bin_dir


def run(tmp_path: Path, mock_bin: Path | None = None,
        args: list[str] | None = None,
        env_content: str | None = VALID_ENV,
        extra_env: dict | None = None,
        cwd: str | None = None) -> subprocess.CompletedProcess:
    """
    Run preflight.sh.

    - Creates config/.env in a temp repo root (PREFLIGHT_REPO_ROOT).
    - Prepends mock_bin to PATH if provided.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "config").mkdir(exist_ok=True)
    (repo_root / "bin").mkdir(exist_ok=True)

    if env_content is not None:
        (repo_root / "config" / ".env").write_text(env_content)

    # Create a minimal shell script in the temp repo so the line-endings check
    # has something to inspect (the glob bin/*.sh would otherwise be empty).
    (repo_root / "bin" / "entrypoint.sh").write_text("#!/bin/bash\necho hello\n")

    env = os.environ.copy()
    env.pop("WSL_DISTRO_NAME", None)  # Never run WSL check unless test explicitly sets it
    env["PREFLIGHT_REPO_ROOT"] = str(repo_root)

    if mock_bin is not None:
        env["PATH"] = f"{mock_bin}:{env.get('PATH', '/usr/bin:/bin')}"

    if extra_env:
        env.update(extra_env)

    cmd = ["bash", str(PREFLIGHT)] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or str(PACKAGE_ROOT),
        timeout=60,
    )


# =============================================================================
# Check 1: docker_engine_reachable
# =============================================================================

class TestDockerEngineReachable:
    def test_docker_ok(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock)
        assert "✓ docker_engine_reachable" in result.stdout or result.returncode == 0
        assert "docker_engine_reachable" in result.stdout

    def test_docker_not_reachable(self, tmp_path):
        """docker exits with non-zero (simulating not installed / not reachable)."""
        mock = make_full_mock_bin(tmp_path)
        # exit 127 is how bash signals "command not found"; any non-0 non-124 triggers
        # the "not reachable" branch in the script.
        write_mock(mock, "docker", "#!/bin/bash\nexit 127\n")
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "docker_engine_reachable" in result.stdout
        assert "not reachable" in result.stdout

    def test_docker_exits_nonzero(self, tmp_path):
        """docker info exits non-zero (not timeout) → not-reachable message."""
        mock = make_full_mock_bin(tmp_path)
        write_mock(mock, "docker", """\
#!/bin/bash
if [ "$1" = "info" ]; then exit 1; fi
echo "Docker Compose version v2.27.0"
""")
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "not reachable" in result.stdout

    @pytest.mark.slow
    def test_docker_info_hangs(self, tmp_path):
        """docker info hangs → fail with 'timed out' message after ~10s."""
        mock = make_full_mock_bin(tmp_path)
        write_mock(mock, "docker", """\
#!/bin/bash
if [ "$1" = "info" ]; then
    sleep 30
fi
echo "Docker Compose version v2.27.0"
""")
        result = run(tmp_path, mock, cwd="/tmp")
        assert result.returncode == 1
        assert "timed out" in result.stdout


# =============================================================================
# Check 2: docker_compose_v2
# =============================================================================

class TestDockerComposeV2:
    def test_compose_v2_pass(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock)
        assert "✓ docker_compose_v2" in result.stdout

    def test_compose_not_found(self, tmp_path):
        """docker compose subcommand not found → fail."""
        mock = make_full_mock_bin(tmp_path)
        write_mock(mock, "docker", """\
#!/bin/bash
if [ "$1" = "info" ]; then
    echo "Docker Root Dir: /var/lib/docker"
    exit 0
elif [ "$1" = "compose" ]; then
    exit 1
fi
""")
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "docker_compose_v2" in result.stdout
        assert "not found" in result.stdout

    def test_compose_v1_fails(self, tmp_path):
        """Docker Compose v1.x is reported → fail."""
        mock = make_full_mock_bin(tmp_path, compose_version="1.29.2")
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "docker_compose_v2" in result.stdout
        assert "not found" in result.stdout


# =============================================================================
# Check 3: wsl_integration_healthy
# =============================================================================

class TestWslIntegrationHealthy:
    def test_no_wsl_distro_name_skips_check(self, tmp_path):
        """Without WSL_DISTRO_NAME, the check doesn't run."""
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock)
        assert "wsl_integration_healthy" not in result.stdout

    def test_wsl_healthy(self, tmp_path):
        """Inside WSL with healthy docker → pass."""
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, extra_env={"WSL_DISTRO_NAME": "Ubuntu"})
        assert "✓ wsl_integration_healthy" in result.stdout

    def test_wsl_permission_denied(self, tmp_path):
        """docker info outputs 'Permission denied' → WSL integration fail."""
        mock = make_full_mock_bin(tmp_path)
        write_mock(mock, "docker", """\
#!/bin/bash
if [ "$1" = "info" ]; then
    echo "Cannot connect to the Docker daemon. Permission denied."
    exit 1
elif [ "$1" = "compose" ] && [ "$2" = "version" ]; then
    echo "Docker Compose version v2.27.0"
    exit 0
fi
""")
        result = run(tmp_path, mock, extra_env={"WSL_DISTRO_NAME": "Ubuntu"})
        assert result.returncode == 1
        assert "wsl_integration_healthy" in result.stdout
        assert "WSL integration" in result.stdout

    @pytest.mark.slow
    def test_wsl_timeout(self, tmp_path):
        """docker info hangs inside WSL → wsl timeout fail."""
        mock = make_full_mock_bin(tmp_path)
        write_mock(mock, "docker", """\
#!/bin/bash
if [ "$1" = "info" ]; then
    sleep 30
elif [ "$1" = "compose" ] && [ "$2" = "version" ]; then
    echo "Docker Compose version v2.27.0"
    exit 0
fi
""")
        result = run(tmp_path, mock, extra_env={"WSL_DISTRO_NAME": "Ubuntu"})
        assert result.returncode == 1
        assert "wsl_integration_healthy" in result.stdout
        assert "timed out" in result.stdout


# =============================================================================
# Check 4: architecture_supported
# =============================================================================

class TestArchitectureSupported:
    @pytest.mark.parametrize("arch", ["x86_64", "aarch64", "arm64"])
    def test_supported_arch_passes(self, tmp_path, arch):
        mock = make_full_mock_bin(tmp_path)
        write_mock(mock, "uname", f"""\
#!/bin/bash
if [ "$1" = "-m" ]; then echo "{arch}"; else /usr/bin/uname "$@"; fi
""")
        result = run(tmp_path, mock)
        assert "✓ architecture_supported" in result.stdout

    def test_unsupported_arch_fails(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        write_mock(mock, "uname", """\
#!/bin/bash
if [ "$1" = "-m" ]; then echo "riscv64"; else /usr/bin/uname "$@"; fi
""")
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "Unsupported architecture: riscv64" in result.stdout


# =============================================================================
# Check 5: shell_script_line_endings
# =============================================================================

class TestShellScriptLineEndings:
    def test_no_crlf_passes(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock)
        assert "✓ shell_script_line_endings" in result.stdout

    def test_crlf_script_fails(self, tmp_path):
        """A shell script with CRLF → fail with line-endings message."""
        mock = make_full_mock_bin(tmp_path, crlf_file="entrypoint.sh")
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "shell_script_line_endings" in result.stdout
        assert "Windows line endings" in result.stdout

    def test_crlf_message_contains_filename(self, tmp_path):
        """Failure message names the problematic file."""
        mock = make_full_mock_bin(tmp_path, crlf_file="entrypoint.sh")
        result = run(tmp_path, mock)
        assert "entrypoint.sh" in result.stdout


# =============================================================================
# Check 6: required_env_vars + .env parser
# =============================================================================

class TestRequiredEnvVars:
    def test_all_vars_present_passes(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock)
        assert "✓ required_env_vars" in result.stdout

    def test_missing_env_file(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, env_content=None)
        assert result.returncode == 1
        assert "required_env_vars" in result.stdout
        assert "missing or incomplete" in result.stdout

    def test_missing_dashboard_port(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        env = "AGENT_SERVER_TOKEN=tok\nDISCORD_BOT_TOKEN_PRIMARY=dt\n"
        result = run(tmp_path, mock, env_content=env)
        assert result.returncode == 1
        assert "required_env_vars" in result.stdout

    def test_missing_agent_server_token(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        env = "DASHBOARD_PORT=3000\nDISCORD_BOT_TOKEN_PRIMARY=dt\n"
        result = run(tmp_path, mock, env_content=env)
        assert result.returncode == 1
        assert "required_env_vars" in result.stdout

    def test_missing_discord_token(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        env = "DASHBOARD_PORT=3000\nAGENT_SERVER_TOKEN=tok\n"
        result = run(tmp_path, mock, env_content=env)
        assert result.returncode == 1
        assert "required_env_vars" in result.stdout

    # --- Parser-specific cases ---

    def test_plain_assignment(self, tmp_path):
        """DASHBOARD_PORT=3000 → matches."""
        mock = make_full_mock_bin(tmp_path)
        env = "DASHBOARD_PORT=3000\nAGENT_SERVER_TOKEN=tok\nDISCORD_BOT_TOKEN_PRIMARY=dt\n"
        result = run(tmp_path, mock, env_content=env)
        assert "✓ required_env_vars" in result.stdout

    def test_export_prefix_stripped(self, tmp_path):
        """export DASHBOARD_PORT=3000 → matches."""
        mock = make_full_mock_bin(tmp_path)
        env = "export DASHBOARD_PORT=3000\nexport AGENT_SERVER_TOKEN=tok\nexport DISCORD_BOT_TOKEN_PRIMARY=dt\n"
        result = run(tmp_path, mock, env_content=env)
        assert "✓ required_env_vars" in result.stdout

    def test_declare_prefix_stripped(self, tmp_path):
        """declare -x DASHBOARD_PORT=3000 → matches."""
        mock = make_full_mock_bin(tmp_path)
        env = "declare -x DASHBOARD_PORT=3000\ndeclare -x AGENT_SERVER_TOKEN=tok\ndeclare -x DISCORD_BOT_TOKEN_PRIMARY=dt\n"
        result = run(tmp_path, mock, env_content=env)
        assert "✓ required_env_vars" in result.stdout

    def test_double_quoted_value_stripped(self, tmp_path):
        """DASHBOARD_PORT="3000" → matches."""
        mock = make_full_mock_bin(tmp_path)
        env = 'DASHBOARD_PORT="3000"\nAGENT_SERVER_TOKEN="tok"\nDISCORD_BOT_TOKEN_PRIMARY="dt"\n'
        result = run(tmp_path, mock, env_content=env)
        assert "✓ required_env_vars" in result.stdout

    def test_single_quoted_value_stripped(self, tmp_path):
        """DASHBOARD_PORT='3000' → matches."""
        mock = make_full_mock_bin(tmp_path)
        env = "DASHBOARD_PORT='3000'\nAGENT_SERVER_TOKEN='tok'\nDISCORD_BOT_TOKEN_PRIMARY='dt'\n"
        result = run(tmp_path, mock, env_content=env)
        assert "✓ required_env_vars" in result.stdout

    def test_comment_line_not_matched(self, tmp_path):
        """# DASHBOARD_PORT=3000 (comment) → treated as missing."""
        mock = make_full_mock_bin(tmp_path)
        env = "# DASHBOARD_PORT=3000\nAGENT_SERVER_TOKEN=tok\nDISCORD_BOT_TOKEN_PRIMARY=dt\n"
        result = run(tmp_path, mock, env_content=env)
        assert result.returncode == 1
        assert "required_env_vars" in result.stdout

    def test_empty_value_not_matched(self, tmp_path):
        """DASHBOARD_PORT= (empty) → treated as missing."""
        mock = make_full_mock_bin(tmp_path)
        env = "DASHBOARD_PORT=\nAGENT_SERVER_TOKEN=tok\nDISCORD_BOT_TOKEN_PRIMARY=dt\n"
        result = run(tmp_path, mock, env_content=env)
        assert result.returncode == 1
        assert "required_env_vars" in result.stdout


# =============================================================================
# Check 7: port_available
# =============================================================================

class TestPortAvailable:
    def test_port_free_passes(self, tmp_path):
        mock = make_full_mock_bin(tmp_path, port_in_use=None)
        result = run(tmp_path, mock)
        assert "✓ port_available" in result.stdout

    def test_port_in_use_fails(self, tmp_path):
        """Port 3000 reported as in use → fail."""
        mock = make_full_mock_bin(tmp_path, port_in_use=3000)
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "port_available" in result.stdout
        assert "already in use" in result.stdout
        assert "3000" in result.stdout

    def test_no_port_check_tools_warns(self, tmp_path):
        """When neither ss nor lsof is available, emit warn instead of silently passing."""
        mock = make_full_mock_bin(tmp_path, port_in_use=None)
        # Override the 'command' builtin via BASH_ENV so that 'command -v ss'
        # and 'command -v lsof' return 1 regardless of PATH, simulating a
        # minimal host where neither port-checking tool is installed.
        bash_env = tmp_path / "no_port_tools_env.sh"
        bash_env.write_text(
            "command() {\n"
            "    if [ \"$1\" = \"-v\" ] && { [ \"$2\" = \"ss\" ] || [ \"$2\" = \"lsof\" ]; }; then\n"
            "        return 1\n"
            "    fi\n"
            "    builtin command \"$@\"\n"
            "}\n"
        )
        result = run(tmp_path, mock, extra_env={"BASH_ENV": str(bash_env)})
        assert result.returncode == 0  # warn exits 0, not fail
        assert "⚠ port_available" in result.stdout
        assert "neither ss nor lsof" in result.stdout


# =============================================================================
# Check 8: disk_space
# =============================================================================

class TestDiskSpace:
    def test_abundant_disk_passes(self, tmp_path):
        mock = make_full_mock_bin(tmp_path, disk_free_gb=50)
        result = run(tmp_path, mock)
        assert "✓ disk_space" in result.stdout

    def test_low_disk_warns(self, tmp_path):
        """Between 5–10GB free → warn, exit 0."""
        mock = make_full_mock_bin(tmp_path, disk_free_gb=8)
        result = run(tmp_path, mock)
        assert result.returncode == 0
        assert "⚠ disk_space" in result.stdout
        assert "PASS (1 warning(s))" in result.stdout

    def test_insufficient_disk_fails(self, tmp_path):
        """Less than 5GB free → fail, exit 1."""
        mock = make_full_mock_bin(tmp_path, disk_free_gb=3)
        result = run(tmp_path, mock)
        assert result.returncode == 1
        assert "✗ disk_space" in result.stdout
        assert "Less than 5GB" in result.stdout


# =============================================================================
# Exit code and summary line
# =============================================================================

class TestExitCodeAndSummary:
    def test_all_pass_exit_0(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock)
        assert result.returncode == 0
        assert "Preflight: PASS" in result.stdout

    def test_warnings_only_exit_0(self, tmp_path):
        """Warnings alone don't flip the exit code."""
        mock = make_full_mock_bin(tmp_path, disk_free_gb=8)
        result = run(tmp_path, mock)
        assert result.returncode == 0
        assert "Preflight: PASS (1 warning(s))" in result.stdout

    def test_any_failure_exit_1(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, env_content=None)  # triggers required_env_vars fail
        assert result.returncode == 1
        assert "Preflight: FAIL" in result.stdout


# =============================================================================
# --quiet flag
# =============================================================================

class TestQuietFlag:
    def test_quiet_suppresses_passes(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--quiet"])
        # Passing checks should not appear
        assert "✓ docker_engine_reachable" not in result.stdout
        assert "✓ architecture_supported" not in result.stdout

    def test_quiet_shows_failures(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--quiet"], env_content=None)
        assert "required_env_vars" in result.stdout
        assert result.returncode == 1

    def test_quiet_shows_warnings(self, tmp_path):
        mock = make_full_mock_bin(tmp_path, disk_free_gb=8)
        result = run(tmp_path, mock, args=["--quiet"])
        assert "⚠ disk_space" in result.stdout

    def test_quiet_still_shows_summary(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--quiet"])
        assert "Preflight: PASS" in result.stdout


# =============================================================================
# --json flag
# =============================================================================

class TestJsonFlag:
    def _parse(self, result: subprocess.CompletedProcess) -> dict:
        assert result.returncode in (0, 1), f"Script crashed:\n{result.stderr}"
        return json.loads(result.stdout)

    def test_valid_json_all_pass(self, tmp_path):
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--json"])
        data = self._parse(result)
        assert isinstance(data["checks"], list)
        assert data["pass"] is True
        assert data["warnings"] == 0
        assert data["failures"] == 0

    def test_json_schema_fields(self, tmp_path):
        """Each check object has name, status, reason."""
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--json"])
        data = self._parse(result)
        for check in data["checks"]:
            assert "name" in check
            assert "status" in check
            assert "reason" in check
            assert check["status"] in ("pass", "warn", "fail")

    def test_json_pass_status_is_null_reason(self, tmp_path):
        """Passing checks have reason: null."""
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--json"])
        data = self._parse(result)
        for check in data["checks"]:
            if check["status"] == "pass":
                assert check["reason"] is None

    def test_json_three_state_warn(self, tmp_path):
        """Warn case: status=warn, top-level warnings=1, pass=true."""
        mock = make_full_mock_bin(tmp_path, disk_free_gb=8)
        result = run(tmp_path, mock, args=["--json"])
        assert result.returncode == 0
        data = self._parse(result)
        assert data["pass"] is True
        assert data["warnings"] == 1
        assert data["failures"] == 0
        warn_checks = [c for c in data["checks"] if c["status"] == "warn"]
        assert len(warn_checks) == 1
        assert warn_checks[0]["name"] == "disk_space"
        assert warn_checks[0]["reason"] is not None

    def test_json_fail_state(self, tmp_path):
        """Fail case: pass=false, failures>0."""
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--json"], env_content=None)
        assert result.returncode == 1
        data = self._parse(result)
        assert data["pass"] is False
        assert data["failures"] >= 1

    def test_json_fail_reason_is_string(self, tmp_path):
        """Failing checks have a non-null reason string."""
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--json"], env_content=None)
        data = self._parse(result)
        fail_checks = [c for c in data["checks"] if c["status"] == "fail"]
        for check in fail_checks:
            assert isinstance(check["reason"], str)
            assert len(check["reason"]) > 0


# =============================================================================
# Working-directory anchoring (BASH_SOURCE)
# =============================================================================

class TestAnchoring:
    def test_runs_correctly_from_outside_repo(self, tmp_path):
        """
        Script anchors to repo root via BASH_SOURCE, not the caller's cwd.
        Run from /tmp (not the repo); the script must still find config/.env
        in PREFLIGHT_REPO_ROOT, not in /tmp/config/.env.
        """
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, cwd="/tmp")
        # The script should read our mock .env successfully.
        # If anchoring were broken it would fail with "config/.env missing".
        assert "✓ required_env_vars" in result.stdout

    def test_no_false_env_failure_from_wrong_cwd(self, tmp_path):
        """
        If the script used ./config/.env (relative to cwd), running from /tmp
        would produce a required_env_vars failure even with a valid .env.
        This test asserts that doesn't happen.
        """
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, cwd="/tmp")
        # Should NOT fail due to missing .env
        fail_checks_in_output = "required_env_vars" in result.stdout and "✗" in result.stdout
        # If anchoring works, required_env_vars passes
        assert "✓ required_env_vars" in result.stdout


# =============================================================================
# --verify-gates
#
# Issue #107: nothing checked that a safety gate can still fire after
# preflight passes. Acceptance test: deliberately disable one gate, then run
# the verifier — it must exit non-zero and name the disabled gate.
# =============================================================================

class TestVerifyGates:
    GATE_NAMES = [
        "docker_engine_reachable",
        "docker_compose_v2",
        "wsl_integration_healthy",
        "architecture_supported",
        "shell_script_line_endings",
        "required_env_vars",
        "port_available",
        "disk_space",
    ]

    def test_all_gates_fail_under_their_own_negative_control(self, tmp_path):
        """
        On the real, unmodified script, every gate must still be able to
        report fail — --verify-gates builds its own fixtures internally, so
        the outer mock_bin/env here only need to satisfy the top-level cd and
        argument parsing, not the gates themselves.
        """
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock, args=["--verify-gates"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert "verify-gates: PASS" in result.stdout
        assert "BROKEN" not in result.stdout
        for name in self.GATE_NAMES:
            assert f"{name} — fails under its negative control" in result.stdout

    def test_disabled_gate_is_named_and_exits_nonzero(self, tmp_path):
        """
        The issue's acceptance test: deliberately disable one gate (the
        household's actual historical bug — a check hardcoded to always
        report pass) and confirm --verify-gates catches it, names it, and
        exits non-zero, without losing coverage of the other gates.
        """
        original = PREFLIGHT.read_text()
        needle = (
            'timeout 10 docker info >/dev/null 2>&1\n'
            '    local rc=$?\n'
            '    if [ "$rc" -eq 0 ]; then\n'
            '        _pass "docker_engine_reachable"'
        )
        replacement = needle.replace('if [ "$rc" -eq 0 ]; then', 'if true; then')
        assert needle in original, "fixture point moved — update this test"
        assert replacement != original

        broken = tmp_path / "preflight-broken.sh"
        broken.write_text(original.replace(needle, replacement, 1))
        broken.chmod(0o755)

        repo_root = tmp_path / "repo"
        (repo_root / "config").mkdir(parents=True)
        (repo_root / "bin").mkdir()
        (repo_root / "config" / ".env").write_text(VALID_ENV)
        (repo_root / "bin" / "entrypoint.sh").write_text("#!/bin/bash\necho hello\n")

        env = os.environ.copy()
        env.pop("WSL_DISTRO_NAME", None)
        env["PREFLIGHT_REPO_ROOT"] = str(repo_root)

        result = subprocess.run(
            ["bash", str(broken), "--verify-gates"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(PACKAGE_ROOT),
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0
        assert "verify-gates: FAIL" in combined
        assert "BROKEN" in combined
        assert "docker_engine_reachable" in combined
        # The other seven gates are untouched and must still prove they can fire.
        for name in self.GATE_NAMES:
            if name == "docker_engine_reachable":
                continue
            assert f"{name} — fails under its negative control" in result.stdout

    def test_normal_mode_unaffected_by_verify_gates_machinery(self, tmp_path):
        """Running without --verify-gates never mentions it."""
        mock = make_full_mock_bin(tmp_path)
        result = run(tmp_path, mock)
        assert result.returncode == 0
        assert "verify-gates" not in result.stdout
