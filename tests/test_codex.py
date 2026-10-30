"""Codex launcher and image contracts (no account or VM required)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tomllib
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_wrapper():
    spec = importlib.util.spec_from_file_location("codex_wrapper", ROOT / "config/codex-wrapper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("args", [
    [], ["login", "--device-auth"], ["exec", "a prompt with spaces", "--", "-s"],
    ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
    ["--approve-for-me"], ["--dangerously-bypass-approvals-and-sandbox"],
    ["-c", 'sandbox_mode="workspace-write"', "--profile", "careful"],
])
def test_launcher_forwards_arguments_without_overriding_choices(args):
    wrapper = load_wrapper()
    with patch.object(wrapper.os, "execv") as execute:
        wrapper.main(args)
    execute.assert_called_once_with(
        "/usr/bin/node", ["node", "/opt/codex/node_modules/@openai/codex/bin/codex.js", *args]
    )


def test_system_defaults_are_overridable_not_cli_flags():
    config = tomllib.loads((ROOT / "config/codex-config.toml").read_text())
    assert config == {
        "sandbox_mode": "danger-full-access", "approval_policy": "never",
        "cli_auth_credentials_store": "file", "check_for_update_on_startup": False,
    }


def test_image_installs_official_pinned_package_and_launcher():
    text = (ROOT / "Containerfile").read_text()
    assert "ARG CODEX_VERSION=0.166.0" in text
    assert 'npm install --prefix /opt/codex --ignore-scripts --no-audit --no-fund' in text
    assert '"@openai/codex@${CODEX_VERSION}"' in text
    assert "COPY config/codex-wrapper.py /usr/local/bin/codex" in text
    assert "COPY config/codex-config.toml /etc/codex/config.toml" in text
    assert "/usr/local/bin/codex" in text.split("RUN chmod 755", 1)[1]


def test_image_adds_managed_context_without_replacing_agent_instructions():
    text = (ROOT / "Containerfile").read_text()
    assert "additional_developer_instructions" in text
    assert "/etc/codex/requirements.toml" in text
    assert 'read_text()' in text
    assert "/etc/tau-sandbox/APPEND_SYSTEM.md" in text
    assert "model_instructions_file" not in text
    assert "AGENTS.md" not in text


def test_generated_requirements_roundtrip_exact_context(tmp_path):
    # Execute the actual image generation expression with temporary paths.
    text = (ROOT / "Containerfile").read_text()
    line = next(line for line in text.splitlines() if line.startswith("RUN python -c "))
    code = line.removeprefix("RUN python -c '").removesuffix("'")
    output = tmp_path / "requirements.toml"
    context = ROOT / "config/APPEND_SYSTEM.md"
    code = code.replace("/etc/codex/requirements.toml", str(output)).replace(
        "/etc/tau-sandbox/APPEND_SYSTEM.md", str(context)
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    assert tomllib.loads(output.read_text()) == {
        "additional_developer_instructions": context.read_text(),
    }
    # Pinned Codex rejects managed instructions above ~40k bytes.
    assert len(context.read_bytes()) + 512 < 40_000


def test_actual_pinned_cli_loads_system_config_and_managed_context(tmp_path):
    """Opt in with a downloaded official CLI; never read the real home."""
    cli = os.environ.get("CODEX_TEST_CLI")
    if not cli:
        pytest.skip("set CODEX_TEST_CLI to official codex-cli 0.166.0")
    assert subprocess.check_output([cli, "--version"], text=True).strip() == "codex-cli 0.166.0"
    if not shutil.which("bwrap"):
        pytest.skip("bwrap required to isolate /etc/codex without root")
    probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--", "true"], capture_output=True)
    if probe.returncode:
        pytest.skip("bwrap user namespaces unavailable")
    config_dir = tmp_path / "system"
    config_dir.mkdir()
    shutil.copyfile(ROOT / "config/codex-config.toml", config_dir / "config.toml")
    context = (ROOT / "config/APPEND_SYSTEM.md").read_text()
    (config_dir / "requirements.toml").write_text(
        "additional_developer_instructions = " + json.dumps(context) + "\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run([
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/etc",
        "--ro-bind", str(config_dir), "/etc/codex", "--bind", str(home), str(home),
        "--", sys.executable, str(ROOT / "tests/codex_config_probe.py"), cli, str(home),
        str(ROOT / "config/APPEND_SYSTEM.md"),
    ], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_cover_codex_auth_and_shared_user_boundary():
    for name in ("README.md", "config/APPEND_SYSTEM.md"):
        text = (ROOT / name).read_text()
        for phrase in (".codex/", "device-auth", "danger-full-access", "additional_developer_instructions"):
            assert phrase in text
        assert "mutually trusted" in text
