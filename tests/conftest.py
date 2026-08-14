"""Shared test infrastructure for tau-sandbox tests."""
import hashlib
import pathlib
import shutil
import subprocess

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
