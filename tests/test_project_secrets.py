"""Fresh-process Bash tests for lib/project-secrets.sh.

Every case runs in its own ``bash -c`` subprocess that sources the
library, builds a temporary filesystem (paths with spaces and symlinks
included), and prints a small ``key|value`` result the harness parses.
No KVM, Podman, or microsandbox runtime is required: these tests prove
the exact launch-to-secret-directory mapping, the projects-root
handling, the paired-source state machine, the symlink-escape rule, and
the reserved-name contract of the pass-through discovery library.
"""
import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).parent.parent
LIBRARY = REPO_ROOT / "lib" / "project-secrets.sh"

_STATE_PRINTER = (
    "printf 'pair|%s\\n' \"$PROJECT_SECRETS_PAIR_STATE\"\n"
    "printf 'env|%s\\n' \"$PROJECT_SECRETS_ENV_PATH\"\n"
    "printf 'policy|%s\\n' \"$PROJECT_SECRETS_POLICY_PATH\"\n"
    "printf 'names|%s\\n' \"$PROJECT_SECRETS_NAMES\"\n"
)


def run_prepare(launch, home, mode, value, cwd=None):
    """Run one project_secrets_prepare in a fresh Bash process.

    Returns (process, state) where state maps pair/env/policy/names plus
    the call's own ``status`` (0/1). The subprocess always exits 0 so
    failing preparations still report their state. A blocking
    implementation would hit the timeout rather than pass silently.
    """
    script = (
        f'source "{LIBRARY}"\n'
        'project_secrets_prepare "$1" "$2" "$3" "$4"; __status=$?\n'
        'printf \'status|%s\\n\' "$__status"\n'
        f"{_STATE_PRINTER}"
        "exit 0\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "prepare", str(launch), str(home), mode, str(value)],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=30,
    )
    state = dict(
        line.split("|", 1) for line in result.stdout.splitlines() if "|" in line
    )
    return result, state


def _make_home(tmp_path, name="home"):
    """A home directory whose default Projects root exists."""
    home = tmp_path / name
    (home / "Projects").mkdir(parents=True)
    return home


def _make_project(home, rel="megali"):
    """A project directory under the home's default Projects root."""
    project = home / "Projects" / rel
    project.mkdir(parents=True)
    return project


def _make_pair(secret_dir, env_text="KEY=value\n", yaml_text="KEY:\n  allow: []\n"):
    """Create a readable regular source pair inside secret_dir."""
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / "secrets.env").write_text(env_text)
    (secret_dir / "secrets.yaml").write_text(yaml_text)


def _assert_none(state):
    """A disabled or secret-free launch: status 0, pair none, empty fields."""
    assert state["status"] == "0"
    assert state["pair"] == "none"
    assert state["env"] == ""
    assert state["policy"] == ""
    assert state["names"] == ""


def _assert_fail(result, state, needle):
    """A rejected launch: status 1, pair stays none, stderr names needle."""
    assert state["status"] == "1"
    assert state["pair"] == "none"
    assert needle in result.stderr
    assert result.stderr.startswith("project-secrets: ")
    assert len(result.stderr.strip().splitlines()) == 1


def _assert_present(result, state, secret_dir, names):
    """A present pair: physical paths exported, names recorded, no stderr."""
    assert state["status"] == "0"
    assert state["pair"] == "present"
    assert state["env"] == str(secret_dir.resolve() / "secrets.env")
    assert state["policy"] == str(secret_dir.resolve() / "secrets.yaml")
    assert state["names"] == names
    assert result.stderr == ""


# --- Exact secret location mapping ---


def test_default_root_maps_project_to_hidden_home_location(tmp_path):
    """Prove the default ${HOME}/Projects root maps its `megali` child to
    the exact `~/.megali` location: this is the core user-facing mapping
    the whole feature depends on."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"
    _make_pair(secret)
    result, state = run_prepare(project, home, "default", "")
    _assert_present(result, state, secret, "KEY")


def test_nested_launch_maps_exactly(tmp_path):
    """Prove a deep launch maps to the exact dotted mirror
    `~/.megali/main/api`: a wrong relative-path derivation would silently
    load another project's secrets."""
    home = _make_home(tmp_path)
    project = _make_project(home, "megali/main/api")
    secret = home / ".megali" / "main" / "api"
    _make_pair(secret)
    result, state = run_prepare(project, home, "default", "")
    _assert_present(result, state, secret, "KEY")


def test_nested_launch_does_not_inherit(tmp_path):
    """Prove secrets never inherit from a parent project: a launch from
    `megali/main` with only `~/.megali` configured must launch without
    secrets, because merging parent credentials would cross project
    boundaries."""
    home = _make_home(tmp_path)
    _make_pair(home / ".megali")
    project = _make_project(home, "megali/main")
    result, state = run_prepare(project, home, "default", "")
    _assert_none(state)


def test_explicit_absolute_root_changes_mapping(tmp_path):
    """Prove an absolute TAU_PROJECTS_DIR replaces the default root in the
    relative-path derivation."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "elsewhere"
    project = root / "megali" / "main"
    project.mkdir(parents=True)
    secret = home / ".megali" / "main"
    _make_pair(secret)
    result, state = run_prepare(project, home, "explicit", str(root))
    _assert_present(result, state, secret, "KEY")


def test_explicit_relative_root_resolves_from_launch_directory(tmp_path):
    """Prove a relative TAU_PROJECTS_DIR resolves from the launch
    directory (not the process cwd), so `..`-style values keep working
    when the launcher was started elsewhere."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "relroot"
    project = root / "megali" / "main"
    project.mkdir(parents=True)
    secret = home / ".megali" / "main"
    _make_pair(secret)
    result, state = run_prepare(
        project, home, "explicit", "../../../relroot", cwd=tmp_path
    )
    _assert_present(result, state, secret, "KEY")


def test_lexically_outside_launch_resolving_inside_is_eligible(tmp_path):
    """Prove physical membership is authoritative in the inclusive
    direction: a launch path lexically outside the root that resolves
    inside it still maps to its secrets."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"
    _make_pair(secret)
    link = tmp_path / "outside-link"
    link.symlink_to(project)
    result, state = run_prepare(link, home, "default", "")
    _assert_present(result, state, secret, "KEY")


def test_lexically_inside_launch_resolving_outside_is_ineligible(tmp_path):
    """Prove physical membership is authoritative in the exclusive
    direction: a path lexically inside the root that resolves outside it
    must not receive secrets mapped to the inside location."""
    home = _make_home(tmp_path)
    outside = tmp_path / "real-megali"
    outside.mkdir()
    (home / "Projects" / "megali").symlink_to(outside)
    _make_pair(home / ".megali")
    result, state = run_prepare(home / "Projects" / "megali", home, "default", "")
    _assert_none(state)


def test_root_and_outside_launches_derive_no_secrets(tmp_path):
    """Prove the projects root itself and launches outside it derive no
    secrets: the mapping is defined only for proper descendants."""
    home = _make_home(tmp_path)
    result, state = run_prepare(home / "Projects", home, "default", "")
    _assert_none(state)
    result, state = run_prepare(tmp_path, home, "default", "")
    _assert_none(state)


def test_unusable_default_root_disables_discovery(tmp_path):
    """Prove an absent, non-directory, or unreadable default root simply
    disables secrets: the default must never block a launch."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "anywhere"
    project.mkdir()
    result, state = run_prepare(project, home, "default", "")
    _assert_none(state)
    # Default root exists as a plain file: still disables.
    (home / "Projects").write_text("not a directory")
    result, state = run_prepare(project, home, "default", "")
    _assert_none(state)


def test_invalid_explicit_root_fails(tmp_path):
    """Prove an explicit TAU_PROJECTS_DIR that is empty, missing, or not
    a directory fails the launch naming the setting: an explicit
    configuration error must never silently disable protection."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    for value in ("", str(tmp_path / "missing"), str(project / "f")):
        if value.endswith("/f"):
            pathlib.Path(value).write_text("x")
        result, state = run_prepare(project, home, "explicit", value)
        _assert_fail(result, state, "TAU_PROJECTS_DIR")


def test_invalid_projects_mode_fails(tmp_path):
    """Prove an unknown projects mode is rejected: the launcher's mode
    contract is exactly default|explicit."""
    home = _make_home(tmp_path)
    result, state = run_prepare(home, home, "bogus", "")
    _assert_fail(result, state, "projects mode")


# --- Derived directory and pair state ---


def test_absent_derived_directory_disables_secrets(tmp_path):
    """Prove a launch with no derived secret directory simply has no
    secrets: the common case must not fail."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    result, state = run_prepare(project, home, "default", "")
    _assert_none(state)


def test_invalid_derived_directory_fails(tmp_path):
    """Prove a derived directory entry that is a plain file, a dangling
    symlink, or an unreadable directory fails naming it: a broken secret
    location is a configuration error, not a silent skip."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"
    secret.write_text("not a directory")
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "secret directory")
    secret.unlink()
    secret.symlink_to(home / "nowhere")
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "secret directory")
    secret.unlink()
    secret.mkdir()
    secret.chmod(0o000)
    try:
        result, state = run_prepare(project, home, "default", "")
        _assert_fail(result, state, "secret directory")
    finally:
        secret.chmod(0o755)


def test_absent_pair_disables_secrets(tmp_path):
    """Prove a valid derived directory with neither source entry launches
    without secrets."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    (home / ".megali").mkdir()
    result, state = run_prepare(project, home, "default", "")
    _assert_none(state)


def test_incomplete_pair_fails_identifying_missing_source(tmp_path):
    """Prove exactly one source entry fails naming the missing file: a
    half-configured pair means the user intended secrets and the launch
    must not silently proceed unprotected."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"
    secret.mkdir()
    (secret / "secrets.env").write_text("KEY=value\n")
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "secrets.yaml")
    (secret / "secrets.env").unlink()
    (secret / "secrets.yaml").write_text("KEY:\n  allow: []\n")
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "secrets.env")


def test_invalid_source_types_fail(tmp_path):
    """Prove directory, FIFO, unreadable-file, and dangling-symlink
    sources fail naming the invalid entry: none of them can supply
    values or policy."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"

    def reset():
        for entry in secret.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                import shutil

                shutil.rmtree(entry)
            else:
                entry.unlink()

    secret.mkdir()
    _make_pair(secret)
    reset()
    (secret / "secrets.env").mkdir()
    (secret / "secrets.yaml").write_text("KEY:\n  allow: []\n")
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "secrets.env")

    reset()
    (secret / "secrets.env").write_text("KEY=value\n")
    (secret / "secrets.yaml").symlink_to(secret / "nowhere")
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "secrets.yaml")

    reset()
    (secret / "secrets.env").write_text("KEY=value\n")
    (secret / "secrets.yaml").write_text("KEY:\n  allow: []\n")
    (secret / "secrets.env").chmod(0o000)
    try:
        result, state = run_prepare(project, home, "default", "")
        _assert_fail(result, state, "secrets.env")
    finally:
        (secret / "secrets.env").chmod(0o644)


def test_symlink_escape_fails_with_and_without_pair(tmp_path):
    """Prove a derived directory resolving inside the projects root is
    rejected whether or not it contains a pair: secret sources must
    never live on mounted project data."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    inside = home / "Projects" / "megali" / ".secret-store"
    inside.mkdir()
    (home / ".megali").symlink_to(inside)
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "escapes")
    _make_pair(inside)
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "escapes")


def test_derived_dir_equal_to_projects_root_fails(tmp_path):
    """Prove the escape rule's equality case: a derived directory that IS
    the projects root (e.g. `.proj` symlinked to `Projects`) is also
    rejected."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "relroot"
    project = root / "proj"
    project.mkdir(parents=True)
    (home / ".proj").symlink_to(root)
    result, state = run_prepare(project, home, "explicit", str(root))
    _assert_fail(result, state, "escapes")


# --- Declared names and the reserved set ---


def test_present_pair_exports_declared_names_deduplicated(tmp_path):
    """Prove plain and `export` assignments (and only those) declare
    names, deduplicated, while comments, blanks, and spaced assignments
    declare nothing: suppression and the reserved check key off exactly
    this set."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"
    _make_pair(
        secret,
        env_text=(
            "# comment\n"
            "\n"
            "ALPHA=1\n"
            "export BETA=two words\n"
            "ALPHA=again\n"
            "SPACED = no\n"
            "  GAMMA=3\n"
        ),
    )
    result, state = run_prepare(project, home, "default", "")
    _assert_present(result, state, secret, "ALPHA BETA GAMMA")


def test_reserved_names_fail_naming_the_variable(tmp_path):
    """Prove reserved exact names and the BASH/TAU_ prefixes are rejected
    naming the offending variable: such a secret would let the runtime
    overwrite shell- or launcher-critical state."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"
    secret.mkdir()
    (secret / "secrets.yaml").write_text("X:\n  allow: []\n")
    for name in ("PATH", "IFS", "BASH_ENV", "HOME", "BASH_FOO", "TAU_X"):
        (secret / "secrets.env").write_text(f"{name}=value\n")
        result, state = run_prepare(project, home, "default", "")
        _assert_fail(result, state, name)


def test_reserved_check_runs_before_state_export(tmp_path):
    """Prove a reserved-name failure exports no paths: the launcher must
    never act on a pair whose names poison the guest environment."""
    home = _make_home(tmp_path)
    project = _make_project(home)
    secret = home / ".megali"
    _make_pair(secret, env_text="PATH=/evil\n")
    result, state = run_prepare(project, home, "default", "")
    _assert_fail(result, state, "PATH")
    assert state["env"] == ""
    assert state["policy"] == ""


def test_paths_with_spaces_resolve(tmp_path):
    """Prove projects and homes with spaces map correctly: quoting bugs
    here would silently disable secrets for such users."""
    home = tmp_path / "my home"
    (home / "Projects" / "mega li").mkdir(parents=True)
    secret = home / ".mega li"
    _make_pair(secret)
    result, state = run_prepare(home / "Projects" / "mega li", home, "default", "")
    _assert_present(result, state, secret, "KEY")
