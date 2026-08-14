"""Security-focused unit tests for run.sh and config files.

These prove at the configuration level (no KVM required) that the sandbox
exposes only declared host paths, keeps host bootstrap sources read-only except
for credentials, isolates writable Tau state, never leaks ambient environment
variables, and rejects dangerous package declarations. Runtime enforcement is
microsandbox's contract and is covered by integration tests.
"""
import os
import pathlib
import subprocess

from test_run import FAKE_MSB, FAKE_PODMAN, invoke_run

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _run_line(msb_log):
    return next(line for line in msb_log if line.startswith("msb run"))


def test_only_expected_paths_are_mounted(tmp_path):
    """The allowlist contains the project, isolated state, immutable prompt,
    read-only bootstrap sources, and the credential-file exception only."""
    (tmp_path / ".env").write_text("")
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    (tau_dir / "settings.json").write_text("{}\n")
    (tau_dir / "credentials.json").write_text("{}\n")
    (tau_dir / "sessions").mkdir()
    (tau_dir / "logs").mkdir()
    (tau_dir / "trust.json").write_text('{"version": 1, "decisions": []}\n')
    outside = tmp_path / "outside-secret"
    outside.write_text("secret\n")
    (tau_dir / "external-link").symlink_to(outside)
    (tmp_path / ".agents").mkdir()

    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    assert result.returncode == 0
    run_line = _run_line(msb_log)
    assert run_line.count(" -v ") == 8
    assert f"-v {tmp_path.resolve()}:/workspace" in run_line
    assert f"tau-persist-{tmp_path.name}-" in run_line and ":/home/tau" in run_line
    assert f"-v {tau_dir.resolve()}:/home/tau/.tau" not in run_line
    assert (
        f"-v {tau_dir.resolve()}/settings.json:"
        "/etc/tau-sandbox/bootstrap/tau/settings.json:ro"
    ) in run_line
    assert f"{tau_dir.resolve()}/settings.json:/home/tau/.tau/settings.json" not in run_line
    assert (
        f"-v {tau_dir.resolve()}/credentials.json:"
        "/etc/tau-sandbox/shared/credentials.json"
    ) in run_line
    assert f"{tau_dir.resolve()}/credentials.json:/home/tau/.tau/credentials.json" not in run_line
    assert f"-v {tmp_path.resolve()}/.agents:/home/tau/.agents:ro" in run_line
    assert f"-v {tau_dir.resolve()}/sessions" not in run_line
    assert f"-v {tau_dir.resolve()}/logs" not in run_line
    assert f"-v {tau_dir.resolve()}/trust.json" not in run_line
    assert str(outside.resolve()) not in run_line
    assert f"-v {tau_dir.resolve()}/external-link" not in run_line
    assert ":/var/lib/tau-sandbox/sessions" in run_line
    assert ":/var/lib/tau-sandbox/logs" in run_line
    assert ":/home/tau/.tau/sessions" not in run_line
    assert ":/home/tau/.tau/logs" not in run_line
    assert "/config/APPEND_SYSTEM.md:/etc/tau-sandbox/APPEND_SYSTEM.md:ro" in run_line


def test_host_config_is_bootstrap_only_except_credentials(tmp_path):
    """Host config mounts read-only outside Tau's writable home path."""
    (tmp_path / ".env").write_text("")
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    (tau_dir / "settings.json").write_text("{}\n")
    (tau_dir / "credentials.json").write_text("{}\n")

    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = _run_line(msb_log)
    assert (
        f"{tau_dir.resolve()}/settings.json:"
        "/etc/tau-sandbox/bootstrap/tau/settings.json:ro"
    ) in run_line
    assert f"{tau_dir.resolve()}/settings.json:/home/tau/.tau/settings.json" not in run_line
    credential_mount = (
        f"{tau_dir.resolve()}/credentials.json:"
        "/etc/tau-sandbox/shared/credentials.json"
    )
    assert credential_mount in run_line
    assert credential_mount + ":ro" not in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=1" in run_line
    assert "/home/tau/.agents" not in run_line


def test_security_profile_and_identity_flags(tmp_path):
    """Every launch pins the restricted profile and unprivileged user."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = _run_line(msb_log)
    assert "--security restricted" in run_line
    assert "--tmpfs /tmp" in run_line
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
