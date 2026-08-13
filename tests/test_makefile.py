"""Unit tests for the Makefile convenience targets."""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
MAKEFILE = REPO_ROOT / "Makefile"


def _text() -> str:
    return MAKEFILE.read_text()


def test_makefile_exists():
    assert MAKEFILE.exists()


def test_makefile_has_required_targets():
    """The documented workflow targets must exist so the README stays honest."""
    for target in ("install", "build", "shell", "tau", "clean", "reset", "images", "volumes"):
        assert target in _text()


def test_makefile_build_pipelines_image():
    """make build must produce a cached image AND load it into msb."""
    text = _text()
    assert "podman build" in text
    assert "podman save" in text
    assert "msb load" in text


def test_makefile_reset_uses_run_script():
    assert "./run.sh --reset" in _text()
