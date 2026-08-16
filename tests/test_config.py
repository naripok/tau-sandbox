"""Unit tests for config/ files: .bashrc, APPEND_SYSTEM.md, entrypoint.sh.

These tests prove the sandbox configuration files exist and describe or
implement persistent install paths, isolated state, host-config bootstrapping,
and invariant environment-reference injection.
"""
import os
import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read(name: str) -> str:
    return (CONFIG_DIR / name).read_text()


# --- .bashrc ---


def test_bashrc_exists():
    assert (CONFIG_DIR / ".bashrc").exists()


def test_bashrc_sets_prompt():
    assert "PS1=" in _read(".bashrc")


def test_bashrc_sets_local_bin_in_path():
    assert '$HOME/.local/bin' in _read(".bashrc")


def test_bashrc_sets_pythonuserbase():
    assert "PYTHONUSERBASE" in _read(".bashrc")


def test_bashrc_sets_npm_config_prefix():
    assert "NPM_CONFIG_PREFIX" in _read(".bashrc")


def test_bashrc_sets_pip_user():
    # pip --user installs land in ~/.local, which persists in the volume.
    assert "PIP_USER=1" in _read(".bashrc")


# --- APPEND_SYSTEM.md ---


def test_append_system_doc_exists():
    assert (CONFIG_DIR / "APPEND_SYSTEM.md").exists()


def test_append_system_doc_describes_filesystem():
    text = _read("APPEND_SYSTEM.md")
    for path in (
        "/workspace",
        "/home/tau",
        "/home/tau/.tau",
        "/home/tau/.tau/sessions",
        "/home/tau/.tau/logs",
        "/home/tau/.agents",
        "/tmp",
    ):
        assert path in text


def test_append_system_doc_describes_ephemeral_rootfs():
    # The microVM rootfs is discarded after every run; the doc must say so.
    assert "ephemeral" in _read("APPEND_SYSTEM.md").lower()


def test_append_system_doc_lists_installed_tools():
    text = _read("APPEND_SYSTEM.md")
    for tool in ("Python", "uv", "Node.js", "tau", "git", "ast-grep", "ripgrep"):
        assert tool in text


def test_append_system_doc_describes_security():
    text = _read("APPEND_SYSTEM.md")
    assert "UID 1000" in text
    assert "hardware-isolated" in text


def test_append_system_doc_describes_packages_file():
    assert ".tau-packages" in _read("APPEND_SYSTEM.md")


def test_append_system_doc_describes_lan_host_egress_rule():
    text = _read("APPEND_SYSTEM.md")
    assert "192.168.15.9" not in text
    assert "TAU_LAN_HOSTS" in text
    assert "other private-network addresses are blocked" in text


# --- entrypoint.sh ---


def test_entrypoint_exists_and_executable():
    path = CONFIG_DIR / "entrypoint.sh"
    assert path.exists()
    assert os.access(path, os.X_OK)


def test_entrypoint_has_required_directives():
    text = _read("entrypoint.sh")
    assert "set -euo pipefail" in text
    assert 'TAU_DIR="$TAU_HOME/.tau"' in text
    assert "rsync" not in text
    assert ".host-config-synced" in text
    assert "LEGACY_BOOTSTRAP_MARKER" in text
    assert "cp -a" in text
    assert "chmod -R u+w" in text
    assert 'rm -rf -- "$destination"' in text
    assert ".tau.msb-root-owned" in text
    assert "link_volume_dir /var/lib/tau-sandbox/sessions" in text
    assert "link_volume_dir /var/lib/tau-sandbox/logs" in text
    assert 'cp -Rn "$legacy/." "$backing/"' in text
    assert "TAU_NO_UPDATE_CHECK" in text
    assert 'exec "$@"' in text


def test_entrypoint_describes_isolated_config_layout():
    text = _read("entrypoint.sh")
    assert "/etc/tau-sandbox/bootstrap/tau" in text
    assert "/home/tau/.tau/credentials.json" in text
    assert "/home/tau/.tau/sessions" in text
    assert "/home/tau/.tau/logs" in text
    assert "/var/lib/tau-sandbox/sessions" in text
    assert "/var/lib/tau-sandbox/logs" in text
    assert "/etc/tau-sandbox/shared/credentials.json" in text
    assert "/home/tau/.agents" in text
    assert "/tau-source" not in text
    assert "APPEND_SYSTEM.md" not in text


def test_entrypoint_sets_persistent_env():
    text = _read("entrypoint.sh")
    assert "PYTHONUSERBASE" in text
    assert "NPM_CONFIG_PREFIX" in text
    assert "HOME=" in text


# --- tau-wrapper.py ---


def test_tau_wrapper_injects_immutable_prompt():
    text = _read("tau-wrapper.py")
    assert "--append-system-prompt" in text
    assert "/etc/tau-sandbox/APPEND_SYSTEM.md" in text
    assert "TAU_SANDBOX_SHARED_CREDENTIALS" in text
    assert "os.fsync" in text


def test_scripts_pass_syntax_checks():
    """Prove shell and Python launcher scripts parse."""
    scripts = [REPO_ROOT / "run.sh", REPO_ROOT / "install.sh", CONFIG_DIR / "entrypoint.sh"]
    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script.name} failed bash -n:\n{result.stderr}"

    result = subprocess.run(
        ["python", "-m", "py_compile", str(CONFIG_DIR / "tau-wrapper.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
