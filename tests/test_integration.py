"""End-to-end integration tests against real microsandbox microVMs.

These tests exercise the full launch path (run.sh -> msb run -> boot ->
entrypoint -> command) and prove the sandbox contract: workspace binding,
isolated persistent state, ephemeral filesystems, bootstrapped host config,
the credential write exception, unprivileged execution, environment forwarding,
and volume isolation between projects.

The test image is built and loaded once per session by the loaded_image
fixture. Tests are skipped when msb or podman is unavailable.
"""
import os
import pathlib
import socket
import subprocess

import pytest

from conftest import TEST_IMAGE_REF, skip_without_msb, volume_names_for


def _host_can_resolve(name: str) -> bool:
    """True when the test host resolves `name` itself — the guest's DNS
    upstreams come from the host's resolvers, so without this the test
    would fail for environmental reasons rather than sandbox regressions."""
    try:
        socket.getaddrinfo(name, 443)
    except OSError:
        return False
    return True

REPO_ROOT = pathlib.Path(__file__).parent.parent


def run_sandbox(project_dir: pathlib.Path, home_dir: pathlib.Path, args, timeout=300):
    """Run run.sh against the session test image with an isolated config.

    HOME is left at the real user home so microsandbox keeps its state
    dir (~/.microsandbox) at a socket-length-safe path; host config reads
    are isolated via the TAU_ENV_FILE/TAU_CONFIG_DIR/TAU_AGENTS_DIR
    overrides into the per-test home_dir.
    """
    env = os.environ.copy()
    env["TAU_IMAGE"] = TEST_IMAGE_REF
    env["TAU_ENV_FILE"] = str(home_dir / ".env-host")
    env["TAU_CONFIG_DIR"] = str(home_dir / ".tau-host")
    env["TAU_AGENTS_DIR"] = str(home_dir / ".agents-host")
    # Only seed the env file if the test did not already write it.
    env_file = home_dir / ".env-host"
    if not env_file.exists():
        env_file.write_text("")
    return subprocess.run(
        [str(REPO_ROOT / "run.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project_dir),
        timeout=timeout,
    )


@pytest.mark.usefixtures("loaded_image", "volume_cleanup")
class TestSandboxBasics:
    """Core launch and identity contract."""

    def test_sandbox_boots_and_runs_command(self, tmp_path, sandbox_home):
        """The full launch path (run.sh -> msb -> microVM -> command) works."""
        result = run_sandbox(tmp_path, sandbox_home, ["echo", "e2e-ok"])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "e2e-ok" in result.stdout

    def test_sandbox_runs_as_unprivileged_user(self, tmp_path, sandbox_home):
        """The in-guest identity is uid 1000, never root."""
        result = run_sandbox(tmp_path, sandbox_home, ["id", "-u"])
        assert result.returncode == 0
        assert result.stdout.strip() == "1000"

    def test_workspace_mount_reads_host_files(self, tmp_path, sandbox_home):
        """Host project files are visible at /workspace."""
        (tmp_path / "marker.txt").write_text("visible-from-host\n")
        result = run_sandbox(tmp_path, sandbox_home, ["cat", "/workspace/marker.txt"])
        assert result.returncode == 0
        assert "visible-from-host" in result.stdout

    def test_workspace_writes_land_on_host(self, tmp_path, sandbox_home):
        """Files written by the guest appear in the host project dir."""
        result = run_sandbox(
            tmp_path, sandbox_home, ["sh", "-c", "echo generated > /workspace/gen.txt"]
        )
        assert result.returncode == 0
        written = (tmp_path / "gen.txt").read_text()
        assert written.strip() == "generated"

    def test_sandbox_resolves_external_hostnames(self, tmp_path, sandbox_home):
        """Outbound DNS must work inside the microVM. Regression: the
        earlier `--net-default-ingress deny` low-level policy silently
        dropped microsandbox's gateway DNS allow rule, so every lookup
        failed with EAI_NONAME and the agent could not reach model APIs.
        The `public` network profile is what restores DNS, so this test
        guards the network flags in run.sh."""
        if not _host_can_resolve("github.com"):
            pytest.skip("host cannot resolve github.com; nothing to compare against")
        result = run_sandbox(tmp_path, sandbox_home, ["getent", "hosts", "github.com"])
        assert result.returncode == 0, f"guest DNS failed: {result.stderr}"
        assert "github.com" in result.stdout

    def test_tau_is_installed(self, tmp_path, sandbox_home):
        """The declared agent is present inside the sandbox."""
        result = run_sandbox(tmp_path, sandbox_home, ["tau", "--version"])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout.strip()

    def test_env_file_variables_reach_the_sandbox(self, tmp_path, sandbox_home):
        """Env-file keys are visible inside the VM."""
        (sandbox_home / ".env-host").write_text("TEST_SANDBOX_ENV=from-host-env\n")
        result = run_sandbox(
            tmp_path, sandbox_home, ["sh", "-c", "echo $TEST_SANDBOX_ENV"]
        )
        assert result.returncode == 0
        assert "from-host-env" in result.stdout


@pytest.mark.usefixtures("loaded_image", "volume_cleanup")
class TestPersistence:
    """Persistent volume and ephemeral rootfs contract."""

    def test_home_volume_persists_across_runs(self, tmp_path, sandbox_home):
        result = run_sandbox(tmp_path, sandbox_home, ["sh", "-c", "echo keep > /home/tau/persist.txt"])
        assert result.returncode == 0
        result = run_sandbox(tmp_path, sandbox_home, ["cat", "/home/tau/persist.txt"])
        assert result.returncode == 0
        assert result.stdout.strip() == "keep"

    def test_nonempty_legacy_state_directory_is_migrated(self, tmp_path, sandbox_home):
        """Existing homes may contain real session directories from the old mount layout."""
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "echo canonical > /var/lib/tau-sandbox/sessions/collision && "
                "rm /home/tau/.tau/sessions && "
                "mkdir /home/tau/.tau/sessions && "
                "echo legacy > /home/tau/.tau/sessions/legacy-session && "
                "echo stale > /home/tau/.tau/sessions/collision",
            ],
        )
        assert result.returncode == 0, result.stderr

        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test -L /home/tau/.tau/sessions && "
                "cat /home/tau/.tau/sessions/legacy-session && "
                "cat /home/tau/.tau/sessions/collision",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["legacy", "canonical"]

    def test_rootfs_is_ephemeral(self, tmp_path, sandbox_home):
        """Rootfs writes vanish on the next run; /home/tau writes survive.

        /tmp is used because /etc is not writable by the unprivileged
        sandbox user — both are equally ephemeral for this check."""
        result = run_sandbox(
            tmp_path, sandbox_home, ["sh", "-c", "echo x > /tmp/root-marker.txt && echo x > /home/tau/home-marker.txt"]
        )
        assert result.returncode == 0
        result = run_sandbox(
            tmp_path, sandbox_home,
            ["sh", "-c", "test -f /tmp/root-marker.txt && echo SURVIVED || echo GONE; test -f /home/tau/home-marker.txt && echo HOME-OK || echo HOME-GONE"],
        )
        assert result.returncode == 0
        assert "GONE" in result.stdout and "HOME-OK" in result.stdout

    def test_volumes_are_isolated_between_projects(self, tmp_path, sandbox_home):
        """Two project dirs get separate volumes; no cross-project reads."""
        proj_a = tmp_path / "proj-a"
        proj_b = tmp_path / "proj-b"
        proj_a.mkdir()
        proj_b.mkdir()
        try:
            (proj_a / ".env").write_text("")
            (proj_b / ".env").write_text("")
            r1 = run_sandbox(proj_a, sandbox_home, ["sh", "-c", "echo secret-a > /home/tau/data.txt"])
            r2 = run_sandbox(proj_b, sandbox_home, ["sh", "-c", "cat /home/tau/data.txt 2>&1 || true"])
            assert r1.returncode == 0 and r2.returncode == 0
            assert "secret-a" not in r2.stdout
        finally:
            subprocess.run(
                ["msb", "volume", "rm", *volume_names_for(str(proj_a.resolve()))],
                capture_output=True,
            )
            subprocess.run(
                ["msb", "volume", "rm", *volume_names_for(str(proj_b.resolve()))],
                capture_output=True,
            )


@pytest.mark.usefixtures("loaded_image", "volume_cleanup")
class TestHostConfigIsolation:
    """Host config seeds writable project state; credentials remain shared."""

    def test_login_tokens_are_available_to_the_agent(self, tmp_path, sandbox_home):
        tau_host = sandbox_home / ".tau-host"
        tau_host.mkdir()
        (tau_host / "credentials.json").write_text(
            '{"openrouter": "sk-fake-token"}\n'
        )
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test -L /home/tau/.tau/credentials.json && "
                "cat /home/tau/.tau/credentials.json",
            ],
        )
        assert result.returncode == 0
        assert "sk-fake-token" in result.stdout

    def test_tau_wrapper_updates_shared_credentials_in_place(self, tmp_path, sandbox_home):
        tau_host = sandbox_home / ".tau-host"
        tau_host.mkdir()
        credentials = tau_host / "credentials.json"
        credentials.write_text('{"openrouter": "old-token"}\n')
        script = (
            "import runpy; "
            "runpy.run_path('/usr/local/bin/tau', run_name='tau_wrapper_test'); "
            "from tau_coding.credentials import FileCredentialStore; "
            "FileCredentialStore().set('openrouter', 'new-token')"
        )
        result = run_sandbox(tmp_path, sandbox_home, ["python", "-c", script])
        assert result.returncode == 0, result.stderr
        assert "new-token" in credentials.read_text()

    def test_host_resources_refresh_each_start_and_are_writable(self, tmp_path, sandbox_home):
        tau_host = sandbox_home / ".tau-host"
        tau_host.mkdir()
        (tau_host / "skills").mkdir()
        (tau_host / "skills" / "hello.md").write_text("# hello\n")
        settings = tau_host / "settings.json"
        settings.write_text('{"host": true}\n')

        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test -w /home/tau/.tau && "
                "test -L /home/tau/.tau/sessions && "
                "test -L /home/tau/.tau/logs && "
                "test -f /home/tau/.tau/skills/hello.md && "
                "cat /home/tau/.tau/settings.json && "
                "printf '{\"sandbox\": true}\\n' > /home/tau/.tau/settings.json && "
                "printf 'local\\n' > /home/tau/.tau/sandbox-only",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert '"host": true' in result.stdout
        assert settings.read_text() == '{"host": true}\n'

        settings.write_text('{"host": "changed"}\n')
        (tau_host / "skills" / "hello.md").unlink()
        (tau_host / "skills" / "new.md").write_text("# new\n")
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "cat /home/tau/.tau/settings.json && "
                "test -f /home/tau/.tau/skills/new.md && "
                "test ! -e /home/tau/.tau/skills/hello.md && "
                "test -f /home/tau/.tau/sandbox-only",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"host": "changed"}'
        assert settings.read_text() == '{"host": "changed"}\n'

    def test_provider_settings_support_atomic_replacement(self, tmp_path, sandbox_home):
        """Regression: a file bind mount returns EBUSY when Tau renames its temp
        file over providers.json. The bootstrapped local copy must be replaceable."""
        tau_host = sandbox_home / ".tau-host"
        tau_host.mkdir()
        providers = tau_host / "providers.json"
        providers.write_text('{"source": "host"}\n')
        command = (
            "temp=$(mktemp /home/tau/.tau/.providers.json.XXXXXX.tmp) && "
            "printf '{\"source\": \"sandbox\"}\\n' > \"$temp\" && "
            "mv \"$temp\" /home/tau/.tau/providers.json && "
            "cat /home/tau/.tau/providers.json"
        )

        result = run_sandbox(tmp_path, sandbox_home, ["sh", "-c", command])
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"source": "sandbox"}'
        assert providers.read_text() == '{"source": "host"}\n'

        result = run_sandbox(
            tmp_path, sandbox_home, ["cat", "/home/tau/.tau/providers.json"]
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"source": "sandbox"}'

    def test_host_agents_are_readonly(self, tmp_path, sandbox_home):
        agents_host = sandbox_home / ".agents-host"
        agents_host.mkdir()
        skill = agents_host / "AGENTS.md"
        skill.write_text("host instructions\n")
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            ["sh", "-c", "! echo changed > /home/tau/.agents/AGENTS.md && echo PROTECTED"],
        )
        assert result.returncode == 0, result.stderr
        assert "PROTECTED" in result.stdout
        assert skill.read_text() == "host instructions\n"

    def test_trust_store_is_project_local(self, tmp_path, sandbox_home):
        tau_host = sandbox_home / ".tau-host"
        tau_host.mkdir()
        host_trust = tau_host / "trust.json"
        host_trust.write_text('{"host": true}\n')
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test ! -e /home/tau/.tau/trust.json && "
                "echo sandbox > /home/tau/.tau/trust.json",
            ],
        )
        assert result.returncode == 0, result.stderr
        result = run_sandbox(
            tmp_path, sandbox_home, ["cat", "/home/tau/.tau/trust.json"]
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "sandbox"
        assert host_trust.read_text() == '{"host": true}\n'

    def test_sessions_and_logs_are_isolated_and_persistent(self, tmp_path, sandbox_home):
        tau_host = sandbox_home / ".tau-host"
        (tau_host / "sessions").mkdir(parents=True)
        (tau_host / "logs").mkdir()
        (tau_host / "sessions" / "host-session").write_text("host\n")
        (tau_host / "logs" / "host-log").write_text("host\n")

        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test ! -e /home/tau/.tau/sessions/host-session && "
                "test ! -e /home/tau/.tau/logs/host-log && "
                "echo sandbox > /home/tau/.tau/sessions/sandbox-session && "
                "echo sandbox > /home/tau/.tau/logs/sandbox-log",
            ],
        )
        assert result.returncode == 0, result.stderr
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "cat /home/tau/.tau/sessions/sandbox-session && "
                "cat /home/tau/.tau/logs/sandbox-log",
            ],
        )
        assert result.returncode == 0
        assert result.stdout.count("sandbox") == 2
        assert not (tau_host / "sessions" / "sandbox-session").exists()
        assert not (tau_host / "logs" / "sandbox-log").exists()

    def test_sandbox_reference_is_immutable_and_does_not_overwrite_host(self, tmp_path, sandbox_home):
        tau_host = sandbox_home / ".tau-host"
        tau_host.mkdir()
        host_append = tau_host / "APPEND_SYSTEM.md"
        host_append.write_text("HOST_APPEND\n")
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "grep -q 'microsandbox microVM' /etc/tau-sandbox/APPEND_SYSTEM.md && "
                "! echo changed > /etc/tau-sandbox/APPEND_SYSTEM.md && "
                "cat /home/tau/.tau/APPEND_SYSTEM.md",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert "HOST_APPEND" in result.stdout
        assert host_append.read_text() == "HOST_APPEND\n"


@pytest.mark.usefixtures("loaded_image")
class TestReset:
    """--reset wipes all per-project volumes."""

    @skip_without_msb
    def test_reset_removes_volume(self, tmp_path, sandbox_home):
        result = run_sandbox(tmp_path, sandbox_home, ["sh", "-c", "echo x > /home/tau/persist.txt"])
        assert result.returncode == 0
        volumes = volume_names_for(str(tmp_path))
        ls = subprocess.run(["msb", "volume", "ls"], capture_output=True, text=True)
        assert all(volume in ls.stdout for volume in volumes)

        result = run_sandbox(tmp_path, sandbox_home, ["--reset"])
        assert result.returncode == 0
        assert "removed" in result.stdout
        ls = subprocess.run(["msb", "volume", "ls"], capture_output=True, text=True)
        assert all(volume not in ls.stdout for volume in volumes)
