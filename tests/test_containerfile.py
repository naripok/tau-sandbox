"""Unit tests for the Containerfile.

Prove the image contract the sandbox depends on: a pinned opencode install,
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
        "nss",
        "chromium",
        "ttf-liberation",
    ):
        assert pkg in text


def test_containerfile_pins_opencode_version():
    # The image is the upgrade vehicle; builds must be deterministic.
    assert "ARG OPENCODE_VERSION=1.18.29" in _text()


def test_containerfile_installs_opencode_from_a_pinned_release():
    # The binary comes from the pinned GitHub release, not an unpinned
    # installer script.
    text = _text()
    assert "github.com/sst/opencode/releases/download" in text
    assert "opencode.ai/install" not in text


def test_containerfile_selects_the_release_asset_by_build_architecture():
    # The release asset is architecture-specific; the build maps uname -m to
    # the glibc asset so arm hosts build a working image too.
    text = _text()
    assert "uname -m" in text
    assert "opencode-linux-${OPENCODE_ARCH}.tar.gz" in text
    assert "x86_64) OPENCODE_ARCH=x64" in text
    assert "aarch64) OPENCODE_ARCH=arm64" in text


def test_containerfile_has_launchers():
    text = _text()
    assert "COPY config/entrypoint.sh" in text
    assert "COPY config/opencode-wrapper.sh /usr/local/bin/opencode" in text
    assert "COPY config/browser.mjs /usr/local/bin/browser" in text
    assert "COPY config/sync-browser-ca.sh /usr/local/bin/sync-browser-ca" in text
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in text


def test_containerfile_has_unprivileged_user_and_no_privileged_bits():
    text = _text()
    assert "useradd -m -u 1000 -s /bin/bash opencode" in text
    assert "USER opencode" in text
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
    assert "mkdir -p /etc/opencode-sandbox/bootstrap/opencode" in text
    assert "/var/lib/opencode-sandbox/sessions" in text
    assert "chown -R opencode:opencode /var/lib/opencode-sandbox" in text
    assert "COPY config/APPEND_SYSTEM.md /etc/opencode-sandbox/APPEND_SYSTEM.md" in text
    assert "COPY config/opencode.json /etc/opencode-sandbox/opencode.json" in text
    assert "COPY config/.bashrc /etc/opencode-sandbox/.bashrc" in text


def test_containerfile_has_no_tau_leftovers():
    # The fork replaces tau end to end; no tau install step survives.
    text = _text()
    assert "naripok/tau" not in text
    assert "TAU_REF" not in text
    assert "tau-wrapper" not in text
    assert "/usr/local/bin/tau" not in text
    assert "/home/tau" not in text
