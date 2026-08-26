"""Unit tests for the Containerfile.

Prove the image contract the sandbox depends on: a pinned Tau install,
the declared tool set, the entrypoint, the unprivileged user, and support
for per-project extra packages via ARG EXTRA_PACKAGES.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
CONTAINERFILE = REPO_ROOT / "Containerfile"


def _text() -> str:
    return CONTAINERFILE.read_text()


def test_containerfile_exists():
    assert CONTAINERFILE.exists()


def test_containerfile_has_required_directives():
    text = _text()
    for directive in ("FROM archlinux:latest", "ENTRYPOINT", "WORKDIR /workspace"):
        assert directive in text


def test_containerfile_has_required_tool_packages():
    text = _text()
    for pkg in (
        "python",
        "python-pip",
        "uv",
        "nodejs",
        "npm",
        "git",
        "openssh",
        "rsync",
        "ast-grep",
        "fd",
        "ripgrep",
        "gcc",
        "make",
        "curl",
    ):
        assert pkg in text


def test_containerfile_installs_tau():
    assert "github.com/naripok/tau" in _text()


def test_containerfile_pins_tau_ref():
    # The image is the upgrade vehicle; builds must be deterministic.
    assert "ARG TAU_REF=" in _text()


def test_containerfile_pins_tau_ref_with_refresh_lock():
    # The pinned commit carries the cross-process OAuth refresh lock.
    assert "ARG TAU_REF=af78d692246f0e814838628a23b1491313985953" in _text()


def test_containerfile_has_launchers():
    text = _text()
    assert "COPY config/entrypoint.sh" in text
    assert "COPY config/tau-wrapper.py /usr/local/bin/tau" in text
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in text


def test_containerfile_has_unprivileged_user_and_no_privileged_bits():
    text = _text()
    assert "useradd -m -u 1000 -s /bin/bash tau" in text
    assert "USER tau" in text
    assert "-perm /6000" in text
    assert "chmod a-s" in text


def test_containerfile_accepts_extra_packages_arg():
    assert 'ARG EXTRA_PACKAGES=""' in _text()


def test_containerfile_has_build_error_handling():
    text = _text()
    assert "package installation failed" in text
    assert "Exit 1" not in text  # the ||{...} block must exit 1
    assert "exit 1" in text


def test_containerfile_copies_sandbox_config():
    text = _text()
    assert "mkdir -p /etc/tau-sandbox/bootstrap/tau" in text
    assert "/var/lib/tau-sandbox/sessions" in text
    assert "chown -R tau:tau /var/lib/tau-sandbox" in text
    assert "COPY config/APPEND_SYSTEM.md /etc/tau-sandbox/APPEND_SYSTEM.md" in text
    assert "COPY config/.bashrc /etc/tau-sandbox/.bashrc" in text
    # Credentials are project-local; the shared host mount dir is gone.
    assert "/etc/tau-sandbox/shared" not in text
