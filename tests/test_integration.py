"""End-to-end integration tests against real microsandbox microVMs.

These tests exercise the full launch path (run.sh -> msb run -> boot ->
entrypoint -> command) and prove the sandbox contract: workspace bind
mount, persistent volume, ephemeral rootfs, host config sync, read-only
config mounts, unprivileged execution, env forwarding, and volume
isolation between projects.

The test image is built and loaded once per session by the loaded_image
fixture. Tests are skipped when msb or podman is unavailable.
"""
import os
import pathlib
import subprocess

import pytest

from conftest import TEST_IMAGE_REF, skip_without_msb, volume_name_for

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
            subprocess.run(["msb", "volume", "rm", volume_name_for(str(proj_a.resolve()))], capture_output=True)
            subprocess.run(["msb", "volume", "rm", volume_name_for(str(proj_b.resolve()))], capture_output=True)


@pytest.mark.usefixtures("loaded_image", "volume_cleanup")
class TestHostConfigSync:
    """Read-only host config mounts and volume syncing."""

    def test_config_mount_is_readonly(self, tmp_path, sandbox_home):
        """Writes to /tau-source must fail (host-side enforced)."""
        (sandbox_home / ".tau-host").mkdir()
        (sandbox_home / ".tau-host" / "skills").mkdir()
        (sandbox_home / ".tau-host" / "skills" / "first.md").write_text("# first\n")
        result = run_sandbox(
            tmp_path, sandbox_home, ["sh", "-c", "echo nope > /tau-source/blocked.txt"]
        )
        assert result.returncode != 0
        # The host file must not have changed either.
        assert not (sandbox_home / ".tau-host" / "blocked.txt").exists()

    def test_host_skills_are_synced_into_volume(self, tmp_path, sandbox_home):
        """New host skills appear in the volume's ~/.tau on the start after
        they are added, and are readable from the guest."""
        (sandbox_home / ".tau-host").mkdir()
        (sandbox_home / ".tau-host" / "skills").mkdir()
        # First boot: syncs host config into the empty volume.
        result = run_sandbox(tmp_path, sandbox_home, ["true"])
        assert result.returncode == 0

        (sandbox_home / ".tau-host" / "skills" / "hello.md").write_text("# hello\n")
        result = run_sandbox(tmp_path, sandbox_home, ["ls", "/home/tau/.tau/skills/"])
        assert result.returncode == 0
        assert "hello.md" in result.stdout

    def test_append_system_doc_is_refreshed(self, tmp_path, sandbox_home):
        """APPEND_SYSTEM.md lands in the volume's ~/.tau for Tau to inject;
        the repo copy (when /workspace is the checkout) takes precedence."""
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "APPEND_SYSTEM.md").write_text(
            "REPO_COPY_MARKER_42\n"
        )
        result = run_sandbox(
            tmp_path, sandbox_home, ["cat", "/home/tau/.tau/APPEND_SYSTEM.md"]
        )
        assert result.returncode == 0
        assert "REPO_COPY_MARKER_42" in result.stdout

    def test_append_system_doc_falls_back_to_image(self, tmp_path, sandbox_home):
        """Without a repo copy, the image-baked environment reference is used."""
        result = run_sandbox(
            tmp_path, sandbox_home, ["sh", "-c", "test -f /home/tau/.tau/APPEND_SYSTEM.md && grep -q 'microsandbox microVM' /home/tau/.tau/APPEND_SYSTEM.md && echo INJECTED || echo MISSING"]
        )
        assert result.returncode == 0
        assert "INJECTED" in result.stdout


@pytest.mark.usefixtures("loaded_image")
class TestReset:
    """--reset wipes the project's persistent volume."""

    @skip_without_msb
    def test_reset_removes_volume(self, tmp_path, sandbox_home):
        result = run_sandbox(tmp_path, sandbox_home, ["sh", "-c", "echo x > /home/tau/persist.txt"])
        assert result.returncode == 0
        volume = volume_name_for(str(tmp_path))
        ls = subprocess.run(["msb", "volume", "ls"], capture_output=True, text=True)
        assert volume in ls.stdout

        result = run_sandbox(tmp_path, sandbox_home, ["--reset"])
        assert result.returncode == 0
        assert "removed" in result.stdout
        ls = subprocess.run(["msb", "volume", "ls"], capture_output=True, text=True)
        assert volume not in ls.stdout
