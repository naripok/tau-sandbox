"""Security-focused unit tests for run.sh and config files.

These prove at the configuration level (no KVM required) that the sandbox
exposes only the declared host paths, mounts shared host config only at the
sandbox home config paths, never leaks environment variables, and rejects
dangerous package declarations. Runtime enforcement (mount containment,
identity virtualization, hardware isolation) is microsandbox's contract,
covered by integration tests.
"""
import os
import pathlib
import subprocess

from test_run import FAKE_MSB, FAKE_PODMAN, invoke_run

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _run_line(msb_log):
    return next(line for line in msb_log if line.startswith("msb run"))


def test_only_expected_dirs_are_mounted(tmp_path):
    """The mount allowlist is exactly: workspace, persistent home volume,
    and (if they exist) the two shared host config dirs mounted into the
    sandbox home. No other host paths may ride into the VM."""
    (tmp_path / ".env").write_text("")
    (tmp_path / ".tau").mkdir()
    (tmp_path / ".agents").mkdir()
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    assert result.returncode == 0
    run_line = _run_line(msb_log)
    # Mount pairs (each -v flag is followed by SOURCE:DEST): workspace,
    # persistent home volume, and the two shared host config dirs.
    assert run_line.count(" -v ") == 4
    assert f"-v {tmp_path.resolve()}:/workspace" in run_line
    assert f"tau-persist-{tmp_path.name}-" in run_line and ":/home/tau" in run_line
    assert f"-v {tmp_path.resolve()}/.tau:/home/tau/.tau" in run_line
    assert f"-v {tmp_path.resolve()}/.agents:/home/tau/.agents" in run_line


def test_host_config_mounts_target_sandbox_home_paths(tmp_path):
    """Shared config mounts must resolve to exactly the sandbox home
    config paths, never a stray host path, and carry no :ro (the sandboxed
    Tau refreshes login tokens in place)."""
    (tmp_path / ".env").write_text("")
    (tmp_path / ".tau").mkdir()
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = _run_line(msb_log)
    assert "-v " + str(tmp_path.resolve() / ".tau") + ":/home/tau/.tau" in run_line
    assert ":ro" not in run_line
    # ~/.agents does not exist here, so no agents mount may be added.
    assert "/home/tau/.agents" not in run_line


def test_security_profile_and_identity_flags(tmp_path):
    """Every launch pins the restricted profile and unprivileged user."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = _run_line(msb_log)
    assert "--security restricted" in run_line
    assert "--user 1000:1000" in run_line


def test_network_profile_closes_inbound_and_enables_dns(tmp_path):
    """Inbound stays closed and gateway DNS is granted via the public
    profile. Regression: the earlier --net-default-ingress deny low-level
    policy dropped microsandbox's auto DNS allow, so every lookup inside
    the VM failed with EAI_NONAME and the agent could not reach model
    APIs. The profile must be used, not the low-level surface."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    assert "--net public" in _run_line(msb_log)
    assert "--net-default-ingress" not in _run_line(msb_log)


def test_env_forwarding_is_limited_to_env_file(tmp_path):
    """Only variables defined in the env file are forwarded — never the
    caller's full environment (which may contain unrelated secrets)."""
    (tmp_path / ".env").write_text("VLLM_API_KEY=from-file\n")
    result, msb_log, _ = invoke_run(
        "bash",
        cwd=tmp_path,
        env={"UNRELATED_SECRET": "must-not-leak"},
    )
    run_line = _run_line(msb_log)
    assert "-e VLLM_API_KEY=from-file" in run_line
    assert "must-not-leak" not in run_line
    assert "UNRELATED_SECRET" not in run_line


def test_env_file_supports_export_lines(tmp_path):
    """Lines written as `export KEY=value` forward identically."""
    (tmp_path / ".env").write_text("export VLLM_API_KEY=exported-key\n")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    assert "-e VLLM_API_KEY=exported-key" in _run_line(msb_log)


def test_packages_file_rejects_command_injection(tmp_path):
    """All shell metacharacters must be rejected before any build runs."""
    for payload in ("cmake; rm -rf /", "cmake$(whoami)", "cmake `id`"):
        (tmp_path / ".tau-packages").write_text(payload + "\n")
        (tmp_path / ".env").write_text("")
        result, msb_log, podman_log = invoke_run("bash", cwd=tmp_path)
        assert result.returncode == 1, f"payload {payload!r} was accepted"
        assert "dangerous characters" in result.stderr
        assert not podman_log, f"build ran for payload {payload!r}"
