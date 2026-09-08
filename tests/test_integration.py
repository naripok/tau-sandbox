"""End-to-end integration tests against real microsandbox microVMs.

These tests exercise the full launch path (run.sh -> msb run -> boot ->
entrypoint -> command) and prove the sandbox contract: workspace binding,
isolated persistent state, ephemeral filesystems, bootstrapped host config,
project-local credentials, unprivileged execution, environment forwarding,
and volume isolation between projects.

The test image is built and loaded once per session by the loaded_image
fixture. Tests are skipped when msb or podman is unavailable.
"""
import os
import pathlib
import socket
import subprocess

import pytest

from conftest import (
    ProjectSecretFixture,
    TEST_IMAGE_REF,
    skip_without_msb,
    skip_without_podman,
    skip_without_virtualization,
    volume_names_for,
)


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


def run_sandbox(
    project_dir: pathlib.Path,
    home_dir: pathlib.Path,
    args,
    timeout=300,
    set_home=False,
):
    """Run run.sh against the session test image with an isolated config.

    HOME is left at the real user home so microsandbox keeps its state
    dir (~/.microsandbox) at a socket-length-safe path; host config reads
    are isolated via the OPENCODE_SANDBOX_ENV_FILE/OPENCODE_SANDBOX_CONFIG_DIR
    overrides into the per-test home_dir.

    set_home=True additionally points HOME itself at home_dir: the exact
    project-secret mapping is relative to $HOME, so present-pair launches
    must run with the fixture home. MSB_HOME keeps microsandbox's state
    directory (image cache, volumes, daemon socket) at the real home so
    the loaded session image and volumes stay reachable at the same
    socket-length-safe path.
    """
    env = os.environ.copy()
    env["OPENCODE_SANDBOX_IMAGE"] = TEST_IMAGE_REF
    env["OPENCODE_SANDBOX_ENV_FILE"] = str(home_dir / ".env-host")
    env["OPENCODE_SANDBOX_CONFIG_DIR"] = str(home_dir / ".opencode-host")
    if set_home:
        env["HOME"] = str(home_dir)
        # Preserve an explicitly inherited MSB_HOME instead of clobbering it;
        # only default microsandbox's state dir to the real home.
        env.setdefault("MSB_HOME", str(pathlib.Path.home() / ".microsandbox"))
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

    def test_opencode_is_installed(self, tmp_path, sandbox_home):
        """The declared agent is present inside the sandbox."""
        result = run_sandbox(tmp_path, sandbox_home, ["opencode", "--version"])
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
        result = run_sandbox(tmp_path, sandbox_home, ["sh", "-c", "echo keep > /home/opencode/persist.txt"])
        assert result.returncode == 0
        result = run_sandbox(tmp_path, sandbox_home, ["cat", "/home/opencode/persist.txt"])
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
                "echo canonical > /var/lib/opencode-sandbox/sessions/collision && "
                "rm /home/opencode/.local/share/opencode/storage && "
                "mkdir /home/opencode/.local/share/opencode/storage && "
                "echo legacy > /home/opencode/.local/share/opencode/storage/legacy-session && "
                "echo stale > /home/opencode/.local/share/opencode/storage/collision",
            ],
        )
        assert result.returncode == 0, result.stderr

        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test -L /home/opencode/.local/share/opencode/storage && "
                "cat /home/opencode/.local/share/opencode/storage/legacy-session && "
                "cat /home/opencode/.local/share/opencode/storage/collision",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["legacy", "canonical"]

    def test_rootfs_is_ephemeral(self, tmp_path, sandbox_home):
        """Rootfs writes vanish on the next run; /home/opencode writes survive.

        /tmp is used because /etc is not writable by the unprivileged
        sandbox user — both are equally ephemeral for this check."""
        result = run_sandbox(
            tmp_path, sandbox_home, ["sh", "-c", "echo x > /tmp/root-marker.txt && echo x > /home/opencode/home-marker.txt"]
        )
        assert result.returncode == 0
        result = run_sandbox(
            tmp_path, sandbox_home,
            ["sh", "-c", "test -f /tmp/root-marker.txt && echo SURVIVED || echo GONE; test -f /home/opencode/home-marker.txt && echo HOME-OK || echo HOME-GONE"],
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
            r1 = run_sandbox(proj_a, sandbox_home, ["sh", "-c", "echo secret-a > /home/opencode/data.txt"])
            r2 = run_sandbox(proj_b, sandbox_home, ["sh", "-c", "cat /home/opencode/data.txt 2>&1 || true"])
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
    """Host config seeds writable project state; credentials stay project-local."""

    def test_project_credentials_live_in_the_home_volume(self, tmp_path, sandbox_home):
        """Host auth.json never seeds the guest, and a project-local
        credential written inside the sandbox persists in the home volume
        across runs while the host file stays untouched."""
        config_host = sandbox_home / ".opencode-host"
        config_host.mkdir()
        host_credentials = config_host / "auth.json"
        host_credentials.write_text('{"openrouter": {"type": "api", "key": "sk-host-token"}}\n')
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test ! -e /home/opencode/.local/share/opencode/auth.json && "
                "mkdir -p /home/opencode/.local/share/opencode && "
                "printf '%s\\n' '{\"openrouter\": {\"type\": \"api\", \"key\": \"sk-project-token\"}}' > "
                "/home/opencode/.local/share/opencode/auth.json",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert host_credentials.read_text() == (
            '{"openrouter": {"type": "api", "key": "sk-host-token"}}\n'
        )
        result = run_sandbox(
            tmp_path, sandbox_home, ["cat", "/home/opencode/.local/share/opencode/auth.json"]
        )
        assert result.returncode == 0, result.stderr
        assert "sk-project-token" in result.stdout

    def test_guest_credential_write_updates_the_project_volume(self, tmp_path, sandbox_home):
        """A guest-side credential write updates the project-local file in
        the persistent home volume (opencode owns the file's format and
        write behavior); the host credential file is never written."""
        config_host = sandbox_home / ".opencode-host"
        config_host.mkdir()
        host_credentials = config_host / "auth.json"
        host_credentials.write_text('{"openrouter": {"type": "api", "key": "host-token"}}\n')
        script = (
            "import json, pathlib; "
            "p = pathlib.Path('/home/opencode/.local/share/opencode/auth.json'); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text(json.dumps({'openrouter': {'type': 'api', 'key': 'new-token'}}))"
        )
        result = run_sandbox(tmp_path, sandbox_home, ["python", "-c", script])
        assert result.returncode == 0, result.stderr
        assert host_credentials.read_text() == (
            '{"openrouter": {"type": "api", "key": "host-token"}}\n'
        )
        result = run_sandbox(
            tmp_path, sandbox_home, ["cat", "/home/opencode/.local/share/opencode/auth.json"]
        )
        assert result.returncode == 0, result.stderr
        assert "new-token" in result.stdout

    def test_host_resources_refresh_each_start_and_are_writable(self, tmp_path, sandbox_home):
        config_host = sandbox_home / ".opencode-host"
        config_host.mkdir()
        (config_host / "skills").mkdir()
        (config_host / "skills" / "hello.md").write_text("# hello\n")
        linked_skill = sandbox_home / "linked-skill"
        linked_skill.mkdir()
        (linked_skill / "SKILL.md").write_text("# linked\n")
        (config_host / "skills" / "linked").symlink_to(
            linked_skill, target_is_directory=True
        )
        settings = config_host / "settings.json"
        settings.write_text('{"host": true}\n')

        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test -w /home/opencode/.config/opencode && "
                "test -L /home/opencode/.local/share/opencode/storage && "
                "test -L /home/opencode/.local/share/opencode/log && "
                "test -f /home/opencode/.config/opencode/skills/hello.md && "
                "test -f /home/opencode/.config/opencode/skills/linked/SKILL.md && "
                "test ! -L /home/opencode/.config/opencode/skills/linked && "
                "cat /home/opencode/.config/opencode/settings.json && "
                "printf '{\"sandbox\": true}\\n' > /home/opencode/.config/opencode/settings.json && "
                "printf 'local\\n' > /home/opencode/.config/opencode/sandbox-only",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert '"host": true' in result.stdout
        assert settings.read_text() == '{"host": true}\n'

        settings.write_text('{"host": "changed"}\n')
        (config_host / "skills" / "hello.md").unlink()
        (config_host / "skills" / "new.md").write_text("# new\n")
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "cat /home/opencode/.config/opencode/settings.json && "
                "test -f /home/opencode/.config/opencode/skills/new.md && "
                "test ! -e /home/opencode/.config/opencode/skills/hello.md && "
                "test -f /home/opencode/.config/opencode/sandbox-only",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"host": "changed"}'
        assert settings.read_text() == '{"host": "changed"}\n'

    def test_provider_settings_support_atomic_replacement(self, tmp_path, sandbox_home):
        """Regression: a file bind mount returns EBUSY when a guest process renames
        its temp file over providers.json. The bootstrapped local copy must be
        replaceable."""
        config_host = sandbox_home / ".opencode-host"
        config_host.mkdir()
        providers = config_host / "providers.json"
        providers.write_text('{"source": "host"}\n')
        command = (
            "temp=$(mktemp /home/opencode/.config/opencode/.providers.json.XXXXXX.tmp) && "
            "printf '{\"source\": \"sandbox\"}\\n' > \"$temp\" && "
            "mv \"$temp\" /home/opencode/.config/opencode/providers.json && "
            "cat /home/opencode/.config/opencode/providers.json"
        )

        result = run_sandbox(tmp_path, sandbox_home, ["sh", "-c", command])
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"source": "sandbox"}'
        assert providers.read_text() == '{"source": "host"}\n'

        result = run_sandbox(
            tmp_path, sandbox_home, ["cat", "/home/opencode/.config/opencode/providers.json"]
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"source": "sandbox"}'

    def test_sessions_and_logs_are_isolated_and_persistent(self, tmp_path, sandbox_home):
        """Session storage and logs live on per-project volumes: guest
        writes persist across runs and never land in the host config
        directory, and a host data directory is never mounted."""
        config_host = sandbox_home / ".opencode-host"
        config_host.mkdir()
        (config_host / "settings.json").write_text("{}\n")
        data_host = sandbox_home / ".opencode-data-host"
        (data_host / "storage").mkdir(parents=True)
        (data_host / "storage" / "host-session").write_text("host\n")

        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "test ! -e /home/opencode/.local/share/opencode/storage/host-session && "
                "echo sandbox > /home/opencode/.local/share/opencode/storage/sandbox-session && "
                "echo sandbox > /home/opencode/.local/share/opencode/log/sandbox-log",
            ],
        )
        assert result.returncode == 0, result.stderr
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "cat /home/opencode/.local/share/opencode/storage/sandbox-session && "
                "cat /home/opencode/.local/share/opencode/log/sandbox-log",
            ],
        )
        assert result.returncode == 0
        assert result.stdout.count("sandbox") == 2
        assert not (config_host / "sessions").exists()
        assert not (config_host / "logs").exists()
        assert not (data_host / "storage" / "sandbox-session").exists()

    def test_sandbox_reference_is_immutable_and_does_not_overwrite_host(self, tmp_path, sandbox_home):
        config_host = sandbox_home / ".opencode-host"
        config_host.mkdir()
        host_append = config_host / "APPEND_SYSTEM.md"
        host_append.write_text("HOST_APPEND\n")
        result = run_sandbox(
            tmp_path,
            sandbox_home,
            [
                "sh",
                "-c",
                "grep -q 'microsandbox microVM' /etc/opencode-sandbox/APPEND_SYSTEM.md && "
                "! echo changed > /etc/opencode-sandbox/APPEND_SYSTEM.md && "
                "cat /home/opencode/.config/opencode/APPEND_SYSTEM.md",
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
        result = run_sandbox(tmp_path, sandbox_home, ["sh", "-c", "echo x > /home/opencode/persist.txt"])
        assert result.returncode == 0
        volumes = volume_names_for(str(tmp_path))
        ls = subprocess.run(["msb", "volume", "ls"], capture_output=True, text=True)
        assert all(volume in ls.stdout for volume in volumes)

        result = run_sandbox(tmp_path, sandbox_home, ["--reset"])
        assert result.returncode == 0
        assert "removed" in result.stdout
        ls = subprocess.run(["msb", "volume", "ls"], capture_output=True, text=True)
        assert all(volume not in ls.stdout for volume in volumes)


@pytest.mark.usefixtures("loaded_image")
@skip_without_msb
@skip_without_podman
@skip_without_virtualization
class TestProjectSecretRuntimeBoundary:
    """Real-runtime project-secret boundary.

    These tests boot the session image through the real launcher and a
    real microsandbox runtime with a present exact-directory secret pair,
    and prove exactly what is observable at this repository's boundary:
    the guest receives the runtime placeholder instead of the source
    value, reserved names are rejected before any boot, and the
    disposable fixture removes its host secret directory and volumes.

    Policy parsing, remote substitution, DNS observation, TLS identity,
    authority checks, and redaction are the runtime's documented
    --secret-conf contract: the launcher passes the user's secrets.yaml
    through unmodified, and this suite deliberately depends on no
    third-party echo service.
    """

    def test_project_secret_is_guest_placeholder_only(self, tmp_path):
        """The runtime gives the guest only the $MSB_<NAME> placeholder
        for a project secret, never the source value.

        This is the feature's core non-disclosure invariant: real
        credential bytes must never reach the guest environment, because
        anything in the guest `env` is readable by arbitrary project
        code."""
        with ProjectSecretFixture(tmp_path) as secrets:
            result = run_sandbox(
                secrets.project_dir,
                secrets.home,
                ["printenv", "TEST_PROJECT_API_KEY"],
                set_home=True,
            )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout.strip() == "$MSB_TEST_PROJECT_API_KEY"
        assert "dummy-project-api-key-123" not in result.stdout
        assert "dummy-project-api-key-123" not in result.stderr

    def test_reserved_names_reject_before_boot(self, tmp_path):
        """Representative reserved-name categories are rejected before any
        sandbox boots.

        Exact runner-owned names (PATH) and the OPENCODE_SANDBOX_ prefix
        (which covers the entrypoint's OPENCODE_SANDBOX_ENTRYPOINT_ scratch
        namespace) belong to the
        shell and launcher: a project secret that took any of them would
        let the runtime overwrite process-startup state, so the launcher
        must reject them before image, environment, and mount work."""
        reserved_names = ("PATH", "OPENCODE_SANDBOX_ENTRYPOINT_STAGE", "BASH_ENV")
        for index, name in enumerate(reserved_names):
            env_text = f"{name}=dummy-project-api-key-123\n"
            yaml_text = (
                f"{name}:\n"
                f'  value: "${{{name}}}"\n'
                "  allow:\n"
                "    - api.example.com\n"
            )
            with ProjectSecretFixture(
                tmp_path / f"reserved-{index}",
                env_text=env_text,
                yaml_text=yaml_text,
            ) as secrets:
                result = run_sandbox(
                    secrets.project_dir,
                    secrets.home,
                    ["sh", "-c", "echo BOOTED"],
                    set_home=True,
                )
            assert result.returncode != 0, f"{name} must be rejected"
            assert "reserved" in result.stderr, result.stderr
            assert name in result.stderr
            assert "BOOTED" not in result.stdout

    def test_external_secret_fixture_and_volumes_are_cleaned(self, tmp_path):
        """Exiting the fixture context removes the host secret directory and
        every derived volume.

        The external secret directory is host-only state that must not
        outlive the test that created it: a stale pair would silently arm
        later launches from the same mapped directory, and stale volumes
        would leak disk and cross-test state."""
        secrets = ProjectSecretFixture(tmp_path)
        with secrets:
            result = run_sandbox(
                secrets.project_dir, secrets.home, ["true"], set_home=True
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            listing = subprocess.run(
                ["msb", "volume", "ls"], capture_output=True, text=True
            )
            assert all(volume in listing.stdout for volume in secrets.volumes)
        assert secrets.cleanup_complete
        assert not secrets.secret_dir.exists()
        listing = subprocess.run(
            ["msb", "volume", "ls"], capture_output=True, text=True
        )
        assert all(volume not in listing.stdout for volume in secrets.volumes)
