"""Unit tests for config/ files: .bashrc, APPEND_SYSTEM.md, entrypoint.sh.

These tests prove the sandbox configuration files exist, are valid bash,
and describe/implement the behaviors the sandbox promises: persistent
user-level install paths, environment reference injection, and the shared
host config mounted into the sandbox home.
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
    for path in ("/workspace", "/home/tau", "/home/tau/.tau", "/home/tau/.agents"):
        assert path in text


def test_append_system_doc_describes_ephemeral_rootfs():
    # The microVM rootfs is discarded after every run; the doc must say so.
    assert "ephemeral" in _read("APPEND_SYSTEM.md")


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


# --- entrypoint.sh ---


def test_entrypoint_exists_and_executable():
    path = CONFIG_DIR / "entrypoint.sh"
    assert path.exists()
    assert os.access(path, os.X_OK)


def test_entrypoint_has_required_directives():
    text = _read("entrypoint.sh")
    assert "set -euo pipefail" in text
    # Config is shared via mounts, not rsync: the entrypoint must address
    # the sandbox home config path and must not resurrect the old sync.
    assert 'TAU_DIR="$TAU_HOME/.tau"' in text
    assert "rsync" not in text
    assert "TAU_NO_UPDATE_CHECK" in text
    assert 'exec "$@"' in text


def test_entrypoint_describes_shared_config_layout():
    # run.sh mounts host config into the sandbox home; the entrypoint must
    # document that layout and not re-introduce a /tau-source rsync path.
    text = _read("entrypoint.sh")
    assert "/home/tau/.tau" in text
    assert "/home/tau/.agents" in text
    assert "/tau-source" not in text


def test_entrypoint_refreshes_append_system():
    text = _read("entrypoint.sh")
    assert "APPEND_SYSTEM.md" in text
    assert "/workspace/config/APPEND_SYSTEM.md" in text
    assert "/etc/tau-sandbox/APPEND_SYSTEM.md" in text


def test_entrypoint_sets_persistent_env():
    text = _read("entrypoint.sh")
    assert "PYTHONUSERBASE" in text
    assert "NPM_CONFIG_PREFIX" in text
    assert "HOME=" in text


def test_shell_scripts_pass_syntax_check():
    """Prove every shell script in the repo parses under bash -n.

    Catches syntax errors in silent code paths (config mounts absent,
    env file missing) that only run in production.
    """
    scripts = [REPO_ROOT / "run.sh", REPO_ROOT / "install.sh", CONFIG_DIR / "entrypoint.sh"]
    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script.name} failed bash -n:\n{result.stderr}"
