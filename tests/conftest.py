"""Shared test infrastructure for tau-sandbox tests."""
import hashlib
import os
import pathlib
import platform
import shutil
import subprocess
import sys

import pytest

TEST_IMAGE = "tau-sandbox-test"
TEST_IMAGE_REF = f"localhost/{TEST_IMAGE}:latest"

skip_without_msb = pytest.mark.skipif(
    not shutil.which("msb"),
    reason="msb not found in PATH",
)
skip_without_podman = pytest.mark.skipif(
    not shutil.which("podman"),
    reason="podman not found in PATH",
)

def _missing_virtualization_prerequisite() -> str | None:
    """The missing hardware-virtualization prerequisite for real-runtime
    integration tests, or None when microsandbox can boot a microVM."""
    if sys.platform.startswith("linux"):
        if not (
            os.path.exists("/dev/kvm")
            and os.access("/dev/kvm", os.R_OK | os.W_OK)
        ):
            return "Linux requires a readable and writable /dev/kvm"
    elif sys.platform == "darwin":
        if platform.machine() != "arm64":
            return "macOS requires Apple Silicon"
    else:
        return f"unsupported platform for microsandbox: {sys.platform}"
    return None


MISSING_VIRTUALIZATION_PREREQUISITE = _missing_virtualization_prerequisite()
skip_without_virtualization = pytest.mark.skipif(
    MISSING_VIRTUALIZATION_PREREQUISITE is not None,
    reason=MISSING_VIRTUALIZATION_PREREQUISITE
    or "hardware virtualization available",
)


@pytest.fixture
def sandbox_home(tmp_path):
    """A fake host $HOME per test so run.sh never reads the real user's
    ~/.env, ~/.tau, or ~/.agents, and so env/config mounts are isolated."""
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture(scope="session")
def loaded_image():
    """Build the test image and load it into the microsandbox cache once
    per session; remove both artifacts on teardown.

    Proves the whole build pipeline works (Containerfile -> podman build ->
    podman save -> msb load) and gives the integration tests a reusable
    image without building per test.
    """
    if not (shutil.which("msb") and shutil.which("podman")):
        pytest.skip("msb and podman required")
    result = subprocess.run(
        ["podman", "build", "-t", TEST_IMAGE, "."],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Image build failed:\n{result.stderr}"
    save = subprocess.Popen(
        ["podman", "save", TEST_IMAGE],
        stdout=subprocess.PIPE,
    )
    load = subprocess.run(
        ["msb", "load"],
        stdin=save.stdout,
        capture_output=True,
        text=True,
    )
    save.wait()
    assert save.returncode == 0, "podman save failed"
    assert load.returncode == 0, f"msb load failed:\n{load.stderr}"
    yield TEST_IMAGE_REF
    subprocess.run(["msb", "rmi", TEST_IMAGE_REF], capture_output=True)
    subprocess.run(["podman", "rmi", TEST_IMAGE], capture_output=True)


def volume_names_for(project_path: str) -> tuple[str, str, str]:
    """Derive the home, session, and log volume names used by run.sh."""
    resolved = str(pathlib.Path(project_path).resolve())
    project_name = pathlib.Path(project_path).name
    # run.sh hashes echo output, including its trailing newline.
    hash_suffix = hashlib.sha256((resolved + "\n").encode()).hexdigest()[:8]
    return (
        f"tau-persist-{project_name}-{hash_suffix}",
        f"tau-sessions-{project_name}-{hash_suffix}",
        f"tau-logs-{project_name}-{hash_suffix}",
    )


def volume_name_for(project_path: str) -> str:
    """Return the home volume name for callers that need that specific mount."""
    return volume_names_for(project_path)[0]


@pytest.fixture
def volume_cleanup(tmp_path):
    """Remove all per-project volumes created for a temporary test project."""
    yield tmp_path, tmp_path
    subprocess.run(
        ["msb", "volume", "rm", *volume_names_for(str(tmp_path))],
        capture_output=True,
    )


# Non-sensitive dummy paired sources: a real runtime receives these exact
# bytes, and the tests assert the guest never sees the dummy value itself.
# The policy uses the runtime-native --secret-conf grammar: the launcher
# passes it through unmodified and the runtime resolves ${NAME} from the
# inherited launcher environment.
PROJECT_SECRET_ENV = "TEST_PROJECT_API_KEY=dummy-project-api-key-123\n"
PROJECT_SECRET_YAML = (
    "TEST_PROJECT_API_KEY:\n"
    '  value: "${TEST_PROJECT_API_KEY}"\n'
    "  allow:\n"
    "    - api.example.com\n"
)


class ProjectSecretFixture:
    """A disposable real-runtime project-secret world.

    Creates one unique home holding a projects root with a single project
    directory and the exact mapped host-only secret directory
    (``$HOME/.<project>``) outside the projects root, containing only
    non-sensitive dummy paired sources. Entering writes the pair; exiting
    removes the host secret directory and every derived volume and records
    the outcome in ``cleanup_complete``.

    This is a plain context manager rather than a pytest fixture on
    purpose: tests keep the object, exit the context, and then assert the
    cleanup from outside teardown, so the removal itself is the behavior
    under test instead of an invisible side effect of fixture teardown.
    """

    def __init__(
        self,
        base_dir,
        env_text=PROJECT_SECRET_ENV,
        yaml_text=PROJECT_SECRET_YAML,
        project_name="secret-project",
    ):
        self.home = pathlib.Path(base_dir) / "secret-home"
        self.project_name = project_name
        self.projects_root = self.home / "Projects"
        self.project_dir = self.projects_root / project_name
        self.secret_dir = self.home / f".{project_name}"
        self.env_text = env_text
        self.yaml_text = yaml_text
        self.cleanup_complete = False

    @property
    def volumes(self) -> tuple[str, str, str]:
        """The home, session, and log volumes run.sh derives for the project."""
        return volume_names_for(str(self.project_dir))

    def activate(self):
        """Return the context manager: entering creates the exact mapped
        pair, exiting removes it and all derived volumes."""
        return self

    def __enter__(self):
        self.project_dir.mkdir(parents=True)
        self.secret_dir.mkdir(parents=True)
        (self.secret_dir / "secrets.env").write_text(self.env_text)
        (self.secret_dir / "secrets.yaml").write_text(self.yaml_text)
        # The isolated host env/config overrides run.sh reads must live in
        # the same home the secret mapping is relative to.
        (self.home / ".env-host").write_text("")
        return self

    def __exit__(self, exc_type, exc, tb):
        self._remove()
        return False

    def _remove(self):
        """Remove the host secret directory and the derived volumes, then
        record whether both are actually gone."""
        shutil.rmtree(self.home, ignore_errors=True)
        subprocess.run(
            ["msb", "volume", "rm", *self.volumes], capture_output=True
        )
        listing = subprocess.run(
            ["msb", "volume", "ls"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # A failed listing must not read as successful cleanup.
        listing_ok = listing.returncode == 0
        self.cleanup_complete = (
            listing_ok
            and not self.secret_dir.exists()
            and not any(volume in listing.stdout for volume in self.volumes)
        )
