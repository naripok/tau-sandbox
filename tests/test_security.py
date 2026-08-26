"""Security-focused unit tests for run.sh and config files.

These prove at the configuration level (no KVM required) that the sandbox
exposes only declared host paths, keeps host bootstrap sources read-only,
isolates writable Tau state, never leaks ambient environment variables, and
rejects dangerous package declarations. Runtime enforcement is microsandbox's
contract and is covered by integration tests.
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
    and read-only bootstrap sources; the host credential file is never
    mounted."""
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
    assert ":/etc/tau-sandbox/bootstrap/tau/settings.json:ro" in run_line
    assert f"-v {tau_dir.resolve()}/settings.json:" not in run_line
    assert f"{tau_dir.resolve()}/settings.json:/home/tau/.tau/settings.json" not in run_line
    assert "credentials.json" not in run_line
    assert "TAU_SANDBOX_SHARED_CREDENTIALS" not in run_line
    assert f"-v {tmp_path.resolve()}/.agents:/home/tau/.agents:ro" in run_line
    assert f"-v {tau_dir.resolve()}/sessions" not in run_line
    assert f"-v {tau_dir.resolve()}/logs" not in run_line
    assert f"-v {tau_dir.resolve()}/trust.json" not in run_line
    assert ":/etc/tau-sandbox/bootstrap/tau/external-link:ro" in run_line
    assert str(outside.resolve()) not in run_line
    assert f"-v {tau_dir.resolve()}/external-link" not in run_line
    assert ":/var/lib/tau-sandbox/sessions" in run_line
    assert ":/var/lib/tau-sandbox/logs" in run_line
    assert ":/home/tau/.tau/sessions" not in run_line
    assert ":/home/tau/.tau/logs" not in run_line
    assert "/config/APPEND_SYSTEM.md:/etc/tau-sandbox/APPEND_SYSTEM.md:ro" in run_line


def test_host_config_is_bootstrap_only(tmp_path):
    """Host config mounts read-only outside Tau's writable home path; the
    host credential file is never mounted."""
    (tmp_path / ".env").write_text("")
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    (tau_dir / "settings.json").write_text("{}\n")
    (tau_dir / "credentials.json").write_text("{}\n")

    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = _run_line(msb_log)
    assert ":/etc/tau-sandbox/bootstrap/tau/settings.json:ro" in run_line
    assert f"{tau_dir.resolve()}/settings.json:" not in run_line
    assert f"{tau_dir.resolve()}/settings.json:/home/tau/.tau/settings.json" not in run_line
    assert "credentials.json" not in run_line
    assert "TAU_SANDBOX_SHARED_CREDENTIALS" not in run_line
    assert "/home/tau/.agents" not in run_line


def test_security_profile_and_identity_flags(tmp_path):
    """Every launch pins the restricted profile and unprivileged user."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = _run_line(msb_log)
    assert "--security restricted" in run_line
    assert "--tmpfs /tmp" in run_line
    assert "--user 1000:1000" in run_line


def test_network_policy_allows_only_configured_lan_hosts(tmp_path):
    """The public profile retains internet and DNS access while exact-IP
    TAU_LAN_HOSTS rules permit only the configured hosts without exposing
    the rest of the private LAN. The variable defaults to empty, so no
    --net-rule is emitted. Inbound remains closed because the launcher
    publishes no ports."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = _run_line(msb_log)
    assert "--net public" in run_line
    assert "--net-rule" not in run_line
    assert "--net private" not in run_line
    assert "--net-default-ingress" not in run_line
    assert " -p " not in run_line
    assert " --port " not in run_line

    result, msb_log, _ = invoke_run(
        "bash", cwd=tmp_path, env={"TAU_LAN_HOSTS": "192.168.1.100"}
    )
    run_line = _run_line(msb_log)
    assert "--net public" in run_line
    assert "--net-rule allow@192.168.1.100" in run_line
    assert "--net-rule allow@192.168.1.101" not in run_line


def test_lan_hosts_rejects_argument_injection(tmp_path):
    """TAU_LAN_HOSTS entries must not smuggle extra msb arguments."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run(
        "bash", cwd=tmp_path, env={"TAU_LAN_HOSTS": "1.2.3.4 --security off"}
    )
    assert result.returncode == 1
    assert "TAU_LAN_HOSTS" in result.stderr
    assert not [line for line in msb_log if line.startswith("msb run")]


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


# --- Project-secret exposure and non-disclosure through the launcher ---

from test_run import (  # noqa: E402
    BASE_IMAGE,
    DUMMY_VALUE,
    _secret_run_line,
    invoke_run,
    make_secret_project,
)


def test_no_launcher_channel_contains_dummy_value(tmp_path):
    """The launcher never causally places a project value in its output,
    argv, image inputs, mounts, or raw guest environment arguments. The
    value exists only in the runtime subprocess environment, which the
    runtime's own --secret-conf handling resolves into placeholders."""
    home, proj, secret = make_secret_project(tmp_path)
    env_log = tmp_path / "msb-env.log"
    # No cached image: the Podman build channel is exercised too.
    result, msb_log, podman_log = invoke_run(
        "bash", cwd=proj, home=home,
        env={"MSB_ENV_LOG": str(env_log)},
    )
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("podman build") for line in podman_log)

    assert DUMMY_VALUE not in result.stdout
    assert DUMMY_VALUE not in result.stderr
    for line in msb_log:
        assert DUMMY_VALUE not in line
    for line in podman_log:
        assert DUMMY_VALUE not in line

    run_line = _secret_run_line(msb_log)
    assert run_line is not None
    assert DUMMY_VALUE not in run_line
    assert "-e KEY=" not in run_line

    # The value reaches only the runtime's process environment.
    runtime_env = env_log.read_text().splitlines()
    assert f"KEY={DUMMY_VALUE}" in runtime_env


def test_secret_hosts_do_not_add_network_rules(tmp_path):
    """Policy destinations never expand sandbox network policy: with no
    TAU_LAN_HOSTS exception the runtime invocation carries no --net-rule at
    all, and an explicit LAN exception remains the only rule added."""
    home, proj, secret = make_secret_project(
        tmp_path,
        yaml_text='KEY:\n  allow:\n    - api.example.com\n    - "*.internal.example"\n',
    )
    result, msb_log, _ = invoke_run(
        "bash", cwd=proj, home=home, images=(BASE_IMAGE,), env={"TAU_LAN_HOSTS": ""},
    )
    assert result.returncode == 0, result.stderr
    run_line = _secret_run_line(msb_log)
    assert run_line is not None
    assert "--net public" in run_line
    assert "--net-rule" not in run_line
    assert "api.example.com" not in run_line
    assert "internal.example" not in run_line

    result, msb_log, _ = invoke_run(
        "bash", cwd=proj, home=home, images=(BASE_IMAGE,),
        env={"TAU_LAN_HOSTS": "10.0.0.5"},
    )
    assert result.returncode == 0, result.stderr
    run_line = _secret_run_line(msb_log)
    assert run_line.count("--net-rule") == 1
    assert "--net-rule allow@10.0.0.5" in run_line
    assert "api.example.com" not in run_line
