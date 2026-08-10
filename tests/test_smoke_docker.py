"""
Smoke tests — Verify the Docker build succeeds and core files are present.

These tests validate that the Dockerfile builds without errors and that the
resulting image contains all expected components. This would have caught both
of Ian's installation issues (missing package-lock.json, invalid route export).
"""

import subprocess
import pytest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent


class TestDockerBuild:
    """Verify Docker image builds successfully."""

    @pytest.mark.slow
    def test_docker_build_succeeds(self):
        """The Docker image should build without errors."""
        result = subprocess.run(
            ["docker", "build", "-t", "karakos-test:smoke", "."],
            cwd=str(PACKAGE_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, (
            f"Docker build failed:\n{result.stderr[-2000:]}"
        )

    @pytest.mark.slow
    def test_docker_build_has_dashboard(self):
        """Built image should contain compiled dashboard."""
        result = subprocess.run(
            [
                "docker", "run", "--rm", "karakos-test:smoke",
                "test", "-d", "/workspace/dashboard/.next",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "Dashboard .next directory missing from image"

    @pytest.mark.slow
    def test_docker_build_has_python_deps(self):
        """Built image should have Python dependencies installed."""
        result = subprocess.run(
            [
                "docker", "run", "--rm", "karakos-test:smoke",
                "python3", "-c", "import aiohttp, aiosqlite, discord; print('ok')",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Python deps missing:\n{result.stderr}"
        )

    @pytest.mark.slow
    def test_docker_build_has_node(self):
        """Built image should have Node.js installed."""
        result = subprocess.run(
            [
                "docker", "run", "--rm", "karakos-test:smoke",
                "node", "--version",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "Node.js not installed in image"
        assert result.stdout.strip().startswith("v"), f"Unexpected node version: {result.stdout}"


class TestFileStructure:
    """Verify required files exist in the repository."""

    @pytest.mark.parametrize("path", [
        "Dockerfile",
        "config/docker-compose.yml",
        "config/supervisord.conf",
        "config/.env.template",
        "config/protected-paths.json",
        "bin/agent-server.py",
        "bin/relay.py",
        "bin/scheduler.py",
        "bin/oneshot.py",
        "bin/entrypoint.sh",
        "bin/capture.py",
        "bin/health-monitor.py",
        "bin/memory-maintenance.py",
        "bin/purge-data.py",
        "bin/summarize-session.py",
        "bin/poke.sh",
        "bin/heartbeat.sh",
        "bin/create-agent.sh",
        "mcp/tools-server.py",
        "setup.sh",
        "install.sh",
        "README.md",
        "requirements.txt",
    ])
    def test_required_file_exists(self, path):
        assert (PACKAGE_ROOT / path).exists(), f"Required file missing: {path}"

    @pytest.mark.parametrize("path", [
        "dashboard/package.json",
        "dashboard/package-lock.json",
        "dashboard/app/layout.tsx",
        "dashboard/app/page.tsx",
        "dashboard/lib/api.ts",
    ])
    def test_dashboard_file_exists(self, path):
        assert (PACKAGE_ROOT / path).exists(), f"Dashboard file missing: {path}"

    def test_scripts_are_executable(self):
        """Shell scripts should have execute permission."""
        non_executable = []
        for script in PACKAGE_ROOT.glob("bin/*.sh"):
            if not os.access(script, os.X_OK):
                non_executable.append(script.name)
        assert not non_executable, (
            f"Scripts not executable: {', '.join(non_executable)}\n"
            f"Fix with: chmod +x bin/{' bin/'.join(non_executable)}"
        )

    def test_setup_is_executable(self):
        assert os.access(PACKAGE_ROOT / "setup.sh", os.X_OK), "setup.sh is not executable"

    def test_install_is_executable(self):
        assert os.access(PACKAGE_ROOT / "install.sh", os.X_OK), "install.sh is not executable"


class TestDockerCompose:
    """Verify docker-compose configuration is valid."""

    def test_compose_config_valid(self):
        """docker compose config should parse without errors.

        Creates a minimal .env if missing, since compose references it.
        """
        import tempfile
        env_path = PACKAGE_ROOT / "config" / ".env"
        created_env = False

        if not env_path.exists():
            env_path.write_text("AGENT_SERVER_TOKEN=test\n")
            created_env = True

        try:
            result = subprocess.run(
                ["docker", "compose", "-f", "config/docker-compose.yml", "config"],
                cwd=str(PACKAGE_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, (
                f"docker compose config failed:\n{result.stderr}"
            )
        finally:
            if created_env:
                env_path.unlink()


class TestDockerfileCopyTargets:
    """Verify that paths referenced in Dockerfile COPY commands exist.

    Prevents build failures like #33 where COPY --from=dashboard-build
    referenced /app/public but no public/ directory existed.
    """

    def test_dashboard_copy_sources_exist(self):
        """Directories copied from dashboard into the image must exist."""
        import re
        dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text()

        # Find COPY --from=dashboard-build /app/<path> dashboard/<path>
        pattern = re.compile(r'COPY\s+--from=dashboard-build\s+/app/(\S+)\s+dashboard/(\S+)')
        for match in pattern.finditer(dockerfile):
            src_path = match.group(1)
            dest_ref = match.group(2)

            # .next and node_modules are build artifacts — skip
            if src_path in (".next", "node_modules"):
                continue

            # For source files/dirs, verify they exist in dashboard/
            local_path = PACKAGE_ROOT / "dashboard" / src_path
            assert local_path.exists(), (
                f"Dockerfile copies dashboard/{src_path} but it doesn't exist. "
                f"Create it or remove the COPY line. (Fixes #33)"
            )


class TestNextjsRouteExports:
    """Verify Next.js route files only export valid handlers.

    This test directly prevents the verifySessionToken export bug
    that broke Ian's build (issue #32).
    """

    VALID_EXPORTS = {
        "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
        # Next.js config exports
        "dynamic", "dynamicParams", "revalidate", "fetchCache",
        "runtime", "preferredRegion", "maxDuration",
        "generateStaticParams", "generateMetadata", "metadata",
    }

    def test_route_files_have_valid_exports(self):
        """Route files should only export valid Next.js handlers."""
        import re

        app_dir = PACKAGE_ROOT / "dashboard" / "app"
        issues = []

        for route_file in app_dir.rglob("route.ts"):
            content = route_file.read_text()
            # Find named exports: export { foo } or export function foo
            # and export async function foo
            export_pattern = re.compile(
                r'export\s+(?:async\s+)?function\s+(\w+)'
                r'|export\s*\{\s*([^}]+)\s*\}'
            )
            for match in export_pattern.finditer(content):
                if match.group(1):
                    name = match.group(1)
                    if name not in self.VALID_EXPORTS:
                        rel = route_file.relative_to(PACKAGE_ROOT)
                        issues.append(f"{rel}: invalid export '{name}'")
                elif match.group(2):
                    for name in match.group(2).split(","):
                        name = name.strip().split(" as ")[0].strip()
                        if name and name not in self.VALID_EXPORTS:
                            rel = route_file.relative_to(PACKAGE_ROOT)
                            issues.append(f"{rel}: invalid export '{name}'")

        assert not issues, (
            "Route files have invalid exports:\n" + "\n".join(issues)
        )


class TestSessionSecretConsistency:
    """Verify SESSION_SECRET is handled correctly across the codebase.

    Catches the split-secret bug where route.ts and lib/api.ts each
    generated their own random SESSION_SECRET, making auth permanently
    broken without the env var set.
    """

    def test_no_duplicate_session_secret_definitions(self):
        """Only lib/api.ts should define SESSION_SECRET."""
        import re

        auth_route = PACKAGE_ROOT / "dashboard" / "app" / "api" / "auth" / "route.ts"
        content = auth_route.read_text()

        # Should NOT have its own SESSION_SECRET definition
        assert "SESSION_SECRET" not in content, (
            "auth/route.ts should not define SESSION_SECRET. "
            "Import generateSessionToken from @/lib/api instead."
        )

    def test_auth_route_imports_from_shared_lib(self):
        """Auth route should import token generation from shared lib."""
        auth_route = PACKAGE_ROOT / "dashboard" / "app" / "api" / "auth" / "route.ts"
        content = auth_route.read_text()

        assert "from \"@/lib/api\"" in content or "from '@/lib/api'" in content, (
            "auth/route.ts should import from @/lib/api for shared session handling"
        )

    def test_setup_generates_session_secret(self):
        """setup.sh must generate SESSION_SECRET in the .env file."""
        setup = (PACKAGE_ROOT / "setup.sh").read_text()

        assert "SESSION_SECRET" in setup, (
            "setup.sh does not generate SESSION_SECRET. "
            "Dashboard sessions will break on every container restart."
        )

    def test_env_template_has_session_secret(self):
        """The .env template should document SESSION_SECRET."""
        template = (PACKAGE_ROOT / "config" / ".env.template").read_text()
        assert "SESSION_SECRET" in template, (
            "SESSION_SECRET missing from .env.template"
        )

    def test_no_random_fallback_in_auth_route(self):
        """Auth route must not have crypto.randomBytes fallback for secrets."""
        auth_route = PACKAGE_ROOT / "dashboard" / "app" / "api" / "auth" / "route.ts"
        content = auth_route.read_text()

        assert "randomBytes" not in content, (
            "auth/route.ts should not generate random secrets. "
            "SESSION_SECRET should come from the environment via lib/api.ts."
        )


import os
