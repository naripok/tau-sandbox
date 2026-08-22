"""Fresh-process Bash tests for lib/project-secrets.sh.

Every case runs in its own ``bash -c`` subprocess that sources the library,
builds a temporary filesystem (paths with spaces and symlinks included),
and prints a small ``key|value`` result the harness parses. No KVM,
Podman, or microsandbox runtime is required: these tests prove the exact
launch-to-secret-directory mapping, the projects-root and secret-directory
state machines, the paired-source type contract, the exposure-descriptor
registry and recursive alias/overlap preflight, the pinned helper and
runtime trust contracts, the strict value and policy grammars with their
reserved-name and exact name-set rules, and the cleanup non-use contract
of the host-side discovery library.
"""
import os
import pathlib
import re
import shutil
import socket
import subprocess
import tempfile
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
LIBRARY = REPO_ROOT / "lib" / "project-secrets.sh"

# The library's public state fields, in documented order.
STATE_FIELDS = (
    "PROJECT_SECRETS_PAIR_STATE",
    "PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL",
    "PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL",
    "PROJECT_SECRETS_DIR_LEXICAL",
    "PROJECT_SECRETS_DIR_PHYSICAL",
    "PROJECT_SECRETS_VALUES_LEXICAL",
    "PROJECT_SECRETS_VALUES_PHYSICAL",
    "PROJECT_SECRETS_POLICY_LEXICAL",
    "PROJECT_SECRETS_POLICY_PHYSICAL",
)

_STATE_PRINTER = "\n".join(
    f'printf \'%s|%s\\n\' "{field}" "${{{field}-}}"' for field in STATE_FIELDS
)


def run_discover(launch, home, mode, value, cwd=None):
    """Run one discovery in a fresh Bash process; return (process, state).

    The subprocess always exits 0 so failing discoveries still report
    their state; the library call's own status is the ``status`` state
    field. Test paths never contain the "|" delimiter, so the
    pipe-joined output is unambiguous. A blocking implementation would
    hit the timeout rather than pass silently.
    """
    script = (
        f'source "{LIBRARY}"\n'
        'project_secrets_discover "$1" "$2" "$3" "$4"; __status=$?\n'
        'printf \'status|%s\\n\' "$__status"\n'
        f"{_STATE_PRINTER}\n"
        "exit 0\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "discover", str(launch), str(home), mode, str(value)],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=30,
    )
    state = dict(line.split("|", 1) for line in result.stdout.splitlines())
    return result, state


def _make_home(tmp_path, name="home"):
    """A home directory whose default Projects root exists."""
    home = tmp_path / name
    (home / "Projects").mkdir(parents=True)
    return home


def _make_pair(secret_dir, env_text="KEY=value\n", yaml_text="secrets: {}\n"):
    """Create a readable regular source pair inside secret_dir."""
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / "secrets.env").write_text(env_text)
    (secret_dir / "secrets.yaml").write_text(yaml_text)


def _assert_none(result, state):
    """Assert a disabled launch succeeds with pair state none and every
    path field empty.

    Absence must be represented only by the state word; a sentinel path
    would let later stages treat a placeholder as a real source."""
    assert state["status"] == "0"
    assert state["PROJECT_SECRETS_PAIR_STATE"] == "none"
    for field in STATE_FIELDS[1:]:
        assert state[field] == "", field


def _assert_failure(result, state, error_class, path=None):
    """Assert an invalid input fails nonzero, reporting only a class and
    path on stderr, and leaves no pair state behind."""
    assert state["status"] != "0"
    assert state["PROJECT_SECRETS_PAIR_STATE"] == ""
    assert error_class in result.stderr
    if path is not None:
        assert str(path) in result.stderr


def _assert_present(state, home, relative, root_lex=None):
    """Assert a present pair exposes exactly the mapped projects root,
    hidden directory, and source paths, in lexical and physical form."""
    if root_lex is None:
        root_lex = f"{home}/Projects"
    dir_lex = f"{home}/.{relative}"
    dir_phys = os.path.realpath(dir_lex)
    assert state["status"] == "0"
    assert state["PROJECT_SECRETS_PAIR_STATE"] == "present"
    assert state["PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL"] == str(root_lex)
    assert state["PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL"] == os.path.realpath(root_lex)
    assert state["PROJECT_SECRETS_DIR_LEXICAL"] == dir_lex
    assert state["PROJECT_SECRETS_DIR_PHYSICAL"] == dir_phys
    assert state["PROJECT_SECRETS_VALUES_LEXICAL"] == f"{dir_lex}/secrets.env"
    assert state["PROJECT_SECRETS_VALUES_PHYSICAL"] == f"{dir_phys}/secrets.env"
    assert state["PROJECT_SECRETS_POLICY_LEXICAL"] == f"{dir_lex}/secrets.yaml"
    assert state["PROJECT_SECRETS_POLICY_PHYSICAL"] == f"{dir_phys}/secrets.yaml"


def test_library_exports_discovery_api(tmp_path):
    """Prove sourcing defines the public functions and empty initial pair
    state, that a caller which never discovers can still call cleanup (the
    reset non-use contract), and that discovery writes nothing to stdout.

    The library must be a pure source-time dependency: no discovery work
    or output happens at source time, cleanup stays callable on launch
    paths that bypass discovery entirely, and stdout remains reserved for
    the caller's own reporting."""
    script = (
        f'source "{LIBRARY}"\n'
        "declare -F project_secrets_discover project_secrets_cleanup >/dev/null || exit 10\n"
        '[ -z "${PROJECT_SECRETS_PAIR_STATE-}" ] || exit 11\n'
        "project_secrets_cleanup || exit 12\n"
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""

    home = tmp_path / "home"
    (home / "Projects").mkdir(parents=True)
    silent = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LIBRARY}"; project_secrets_discover "$1" "$2" default ""',
            "discover",
            str(home / "Projects"),
            str(home),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert silent.returncode == 0, silent.stderr
    assert silent.stdout == ""


def test_project_and_nested_mapping_are_exact(tmp_path):
    """Prove the exact hidden mapping: a launch at Projects/<name> selects
    ~/.<name>, a nested launch selects only ~/.<name>/<rest>, and a nested
    launch never inherits the parent project's pair.

    The mapping is the isolation boundary between a project tree and its
    secret sources; an off-by-one level would attach one project's sources
    to a different launch. Spaces in names prove matching is
    component-based rather than naive string prefixing."""
    home = _make_home(tmp_path)
    (home / "Projects" / "mega li" / "main").mkdir(parents=True)
    _make_pair(home / ".mega li")
    _make_pair(home / ".mega li" / "main")

    _, state = run_discover(home / "Projects" / "mega li", home, "default", "")
    _assert_present(state, home, "mega li")

    _, state = run_discover(home / "Projects" / "mega li" / "main", home, "default", "")
    _assert_present(state, home, "mega li/main")

    # No inheritance: the parent's pair must not serve a nested launch.
    shutil.rmtree(home / ".mega li" / "main")
    result, state = run_discover(home / "Projects" / "mega li" / "main", home, "default", "")
    _assert_none(result, state)


def test_projects_root_and_outside_launch_select_none(tmp_path):
    """Prove the projects root itself and any launch outside it select no
    secrets, while a proper descendant still maps.

    Launching exactly at the root or outside it must not derive any hidden
    directory: membership is the gate for all secret selection."""
    home = _make_home(tmp_path)
    (home / "Projects" / "mega li").mkdir(parents=True)
    _make_pair(home / ".mega li")
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    result, state = run_discover(home / "Projects", home, "default", "")
    _assert_none(result, state)

    result, state = run_discover(outside, home, "default", "")
    _assert_none(result, state)

    _, state = run_discover(home / "Projects" / "mega li", home, "default", "")
    _assert_present(state, home, "mega li")


def test_relative_explicit_root_resolves_from_launch_directory(tmp_path):
    """Prove a relative TAU_PROJECTS_DIR value resolves against the launch
    directory, not the Bash working directory, and that an explicit root
    redefines membership while the hidden mapping stays under the home.

    Resolving from the wrong base would silently select or reject the
    wrong projects root. The harness runs with a different cwd than the
    launch directory, so a cwd-based implementation resolves elsewhere and
    fails."""
    home = _make_home(tmp_path)
    root = tmp_path / "pro jects"
    launch = root / "megali" / "main"
    launch.mkdir(parents=True)
    _make_pair(home / ".megali" / "main")
    cwd = root / "megali"

    # "../.." from the launch directory is the root; from cwd it is tmp_path.
    _, state = run_discover(launch, home, "explicit", "../..", cwd=cwd)
    _assert_present(state, home, "megali/main", root_lex=root)

    _, state = run_discover(launch, home, "explicit", root)
    _assert_present(state, home, "megali/main", root_lex=root)


def test_physical_membership_wins_symlink_disagreement(tmp_path):
    """Prove physical location is authoritative for membership: a
    lexically outside launch that physically resolves inside the projects
    root still maps, and a lexically inside launch that physically
    resolves outside is disabled.

    Trusting the lexical spelling would let a symlink either fake project
    membership or smuggle an outside tree into a project launch; both
    disagreement directions are pinned here."""
    # Lexical-out / physical-in: an alias outside the root resolves inside.
    home = _make_home(tmp_path)
    (home / "Projects" / "megali" / "main").mkdir(parents=True)
    _make_pair(home / ".megali" / "main")
    alias = tmp_path / "alias dir"
    alias.symlink_to(home / "Projects" / "megali" / "main")
    _, state = run_discover(alias, home, "default", "")
    _assert_present(state, home, "megali/main")

    # Lexical-in / physical-out: the lexical root is a symlink and the
    # launch resolves outside the physical root.
    home2 = tmp_path / "home2"
    home2.mkdir()
    real_root = tmp_path / "real root"
    (real_root / "megali").mkdir(parents=True)
    outside = tmp_path / "outside tree"
    outside.mkdir()
    (real_root / "escape").symlink_to(outside)
    (home2 / "Projects").symlink_to(real_root)
    result, state = run_discover(home2 / "Projects" / "escape", home2, "default", "")
    _assert_none(result, state)


def test_default_root_states(tmp_path):
    """Prove the default projects-root state machine: an absent root only
    disables discovery, while an existing dangling, non-directory,
    unreadable, or unsearchable entry fails closed.

    A broken default root must never silently downgrade to "no secrets";
    that would hide a real misconfiguration while the user believes
    project secrets are active."""

    def fresh(name):
        home = tmp_path / name
        home.mkdir()
        return home

    launch = tmp_path / "launch"
    launch.mkdir()

    home = fresh("absent")
    result, state = run_discover(launch, home, "default", "")
    _assert_none(result, state)

    home = fresh("dangling")
    (home / "Projects").symlink_to(tmp_path / "missing")
    result, state = run_discover(launch, home, "default", "")
    _assert_failure(result, state, "invalid-projects-root", home / "Projects")

    home = fresh("file")
    (home / "Projects").write_text("not a directory\n")
    result, state = run_discover(launch, home, "default", "")
    _assert_failure(result, state, "invalid-projects-root", home / "Projects")

    home = fresh("unreadable")
    (home / "Projects").mkdir()
    (home / "Projects").chmod(0o300)
    result, state = run_discover(launch, home, "default", "")
    (home / "Projects").chmod(0o755)
    _assert_failure(result, state, "invalid-projects-root", home / "Projects")

    home = fresh("unsearchable")
    (home / "Projects").mkdir()
    (home / "Projects").chmod(0o644)
    result, state = run_discover(launch, home, "default", "")
    (home / "Projects").chmod(0o755)
    _assert_failure(result, state, "invalid-projects-root", home / "Projects")


def test_explicit_root_states(tmp_path):
    """Prove every explicit TAU_PROJECTS_DIR state fails closed: empty,
    missing, dangling, non-directory, unreadable, and unsearchable values
    all reject the launch with an error identifying the setting.

    An explicit root is a deliberate configuration choice, so any invalid
    value is an error rather than a silent no-secrets launch; the error
    must name TAU_PROJECTS_DIR so the user can fix the setting."""
    home = tmp_path / "home"
    home.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    value = tmp_path / "pro jects"

    result, state = run_discover(launch, home, "explicit", "")
    _assert_failure(result, state, "invalid-TAU_PROJECTS_DIR")

    result, state = run_discover(launch, home, "explicit", tmp_path / "missing")
    _assert_failure(result, state, "invalid-TAU_PROJECTS_DIR", tmp_path / "missing")

    value.symlink_to(tmp_path / "no target")
    result, state = run_discover(launch, home, "explicit", value)
    _assert_failure(result, state, "invalid-TAU_PROJECTS_DIR", value)
    value.unlink()

    value.write_text("not a directory\n")
    result, state = run_discover(launch, home, "explicit", value)
    _assert_failure(result, state, "invalid-TAU_PROJECTS_DIR", value)
    value.unlink()

    value.mkdir()
    value.chmod(0o300)
    result, state = run_discover(launch, home, "explicit", value)
    value.chmod(0o755)
    _assert_failure(result, state, "invalid-TAU_PROJECTS_DIR", value)

    value.chmod(0o644)
    result, state = run_discover(launch, home, "explicit", value)
    value.chmod(0o755)
    _assert_failure(result, state, "invalid-TAU_PROJECTS_DIR", value)


def test_secret_directory_states(tmp_path):
    """Prove the derived secret-directory state machine: an absent
    directory only disables discovery, while a dangling, non-directory,
    unreadable, or unsearchable existing entry fails closed.

    The derived location is where secret sources would be read from, so a
    malformed entry must stop the launch instead of being treated as "no
    secrets"; the user may have intended a present pair there."""
    home = _make_home(tmp_path)
    launch = home / "Projects" / "megali"
    launch.mkdir(parents=True)
    secret_dir = home / ".megali"

    result, state = run_discover(launch, home, "default", "")
    _assert_none(result, state)

    secret_dir.symlink_to(tmp_path / "missing")
    result, state = run_discover(launch, home, "default", "")
    _assert_failure(result, state, "invalid-secret-directory", secret_dir)
    secret_dir.unlink()

    secret_dir.write_text("not a directory\n")
    result, state = run_discover(launch, home, "default", "")
    _assert_failure(result, state, "invalid-secret-directory", secret_dir)
    secret_dir.unlink()

    secret_dir.mkdir()
    secret_dir.chmod(0o300)
    result, state = run_discover(launch, home, "default", "")
    secret_dir.chmod(0o755)
    _assert_failure(result, state, "invalid-secret-directory", secret_dir)

    secret_dir.chmod(0o644)
    result, state = run_discover(launch, home, "default", "")
    secret_dir.chmod(0o755)
    _assert_failure(result, state, "invalid-secret-directory", secret_dir)


def test_pair_states_and_exact_basenames(tmp_path):
    """Prove the paired-source contract: only the exact basenames
    secrets.env and secrets.yaml form a pair; both must be readable
    regular files; differently named files never substitute; both absent
    disables; and one-sided, dangling, directory, FIFO, socket, device, or
    unreadable entries fail without opening special files and without
    echoing source contents.

    Exact names stop a project from aliasing a second source into the
    launch; type checks stop special files from blocking discovery or
    leaking through reads (a blocking implementation hits the harness
    timeout); the redaction checks keep diagnostics to class and path
    only."""
    home = _make_home(tmp_path)
    launch = home / "Projects" / "megali"
    launch.mkdir(parents=True)
    secret_dir = home / ".megali"
    secret_dir.mkdir()
    env = secret_dir / "secrets.env"
    policy = secret_dir / "secrets.yaml"

    def discover():
        return run_discover(launch, home, "default", "")

    # Both exact sources present: a present pair with exact paths.
    _make_pair(secret_dir)
    _, state = discover()
    _assert_present(state, home, "megali")
    env.unlink()
    policy.unlink()

    # Differently named files never substitute for the exact pair.
    (secret_dir / "secrets.env.txt").write_text("KEY=value\n")
    (secret_dir / "policy.yaml").write_text("secrets: {}\n")
    result, state = discover()
    _assert_none(result, state)
    (secret_dir / "secrets.env.txt").unlink()
    (secret_dir / "policy.yaml").unlink()

    # An empty directory: both exact entries absent.
    result, state = discover()
    _assert_none(result, state)

    # One-sided pairs identify the missing exact source and never echo
    # the present file's contents.
    env.write_text("TOPSECRETPATTERN=value\n")
    result, state = discover()
    _assert_failure(result, state, "missing-secret-source", policy)
    assert "TOPSECRETPATTERN" not in result.stdout + result.stderr
    env.unlink()

    policy.write_text("secrets: {}\n")
    result, state = discover()
    _assert_failure(result, state, "missing-secret-source", env)

    # Present-but-invalid entries reject by type, without opening them.
    env.symlink_to(tmp_path / "missing target")
    result, state = discover()
    _assert_failure(result, state, "invalid-secret-source", env)
    env.unlink()

    env.mkdir()
    result, state = discover()
    _assert_failure(result, state, "invalid-secret-source", env)
    env.rmdir()

    os.mkfifo(env)
    result, state = discover()
    _assert_failure(result, state, "invalid-secret-source", env)
    env.unlink()

    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(env))
    sock.close()
    result, state = discover()
    _assert_failure(result, state, "invalid-secret-source", env)
    env.unlink()

    env.symlink_to("/dev/null")
    result, state = discover()
    _assert_failure(result, state, "invalid-secret-source", env)
    env.unlink()

    env.write_text("TOPSECRETPATTERN=value\n")
    env.chmod(0o000)
    result, state = discover()
    env.chmod(0o644)
    _assert_failure(result, state, "invalid-secret-source", env)
    assert "TOPSECRETPATTERN" not in result.stdout + result.stderr
    env.unlink()


def test_cleanup_idempotent_with_no_staging(tmp_path):
    """Prove cleanup is an idempotent no-op today: it succeeds before and
    after discovery, twice in a row, and leaves discovery state untouched.

    The launcher composes cleanup into every exit path, including reset
    paths that never discover secrets; a failing or state-mutating cleanup
    would break those callers, and later staging work must extend — not
    change — this contract."""
    home = _make_home(tmp_path)
    (home / "Projects" / "megali").mkdir(parents=True)
    _make_pair(home / ".megali")
    launch = home / "Projects" / "megali"

    script = (
        f'source "{LIBRARY}"\n'
        "project_secrets_cleanup || exit 10\n"
        'project_secrets_discover "$1" "$2" default ""\n'
        f"{_STATE_PRINTER}\n"
        "project_secrets_cleanup || exit 11\n"
        "project_secrets_cleanup || exit 12\n"
        f"{_STATE_PRINTER}\n"
        "exit 0\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "cleanup", str(launch), str(home)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    half = len(STATE_FIELDS)
    before = dict(line.split("|", 1) for line in lines[:half])
    after = dict(line.split("|", 1) for line in lines[half:])
    assert before["PROJECT_SECRETS_PAIR_STATE"] == "present"
    assert before == after


# ---------------------------------------------------------------------------
# Shared exposure descriptors and runtime trust.

_SYSTEM_PATH = os.environ.get("PATH", "/usr/bin:/bin")

_HELPER_TOOLS = ("tr", "cmp", "mktemp", "chmod", "rm")


def run_library(body, args=(), timeout=30):
    """Run one fresh-Bash exercise of the library; return the process.

    The subprocess sources the library once and receives every input as a
    positional parameter so paths with spaces stay intact. A blocking
    implementation hits the timeout instead of passing silently."""
    script = f'source "{LIBRARY}"\n{body}'
    return subprocess.run(
        ["bash", "-c", script, "library", *(str(a) for a in args)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _steps(result):
    """Parse the ``name|value`` lines a library body printed."""
    return {
        line.split("|", 1)[0]: line.split("|", 1)[1]
        for line in result.stdout.splitlines()
        if "|" in line
    }


def _present_pair(base, project="megali"):
    """A present-pair environment; return (home, launch)."""
    home = base / "home"
    launch = home / "Projects" / project
    launch.mkdir(parents=True)
    _make_pair(home / f".{project}")
    return home, launch


def _shim(directory, name, body):
    """Write one executable shim and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def _msb_shim(directory, log, output=b"msb 0.6.12\n", status=0, stderr=b""):
    """A fake runtime executable that logs its invocation and prints a
    controlled ``--version`` result."""
    body = "#!/bin/bash\n"
    body += f'printf \'%s\\n\' "$0 $*" >> "{log}"\n'
    if output:
        body += "printf '" + "".join(f"\\x{b:02x}" for b in output) + "'\n"
    if stderr:
        body += "printf '" + "".join(f"\\x{b:02x}" for b in stderr) + "' >&2\n"
    body += f"exit {status}\n"
    return _shim(directory, "msb", body)


def _helper_shims(directory, log):
    """Functional, invocation-logging shims for the five allowed helpers."""
    return {
        tool: _shim(
            directory,
            tool,
            "#!/bin/bash\n"
            f'printf \'%s\\n\' "$(basename "$0") $*" >> "{log}"\n'
            f'exec "{shutil.which(tool)}" "$@"\n',
        )
        for tool in _HELPER_TOOLS
    }


def _preflight_prologue():
    """Discover a present pair and register the projects-root scan."""
    return (
        'project_secrets_discover "$1" "$2" default ""\n'
        'printf \'discover|%s\\n\' "$?"\n'
        "project_secrets_register_projects_root_scan\n"
        'printf \'rootscan|%s\\n\' "$?"\n'
    )


def _register_only(kind, path):
    """Register one exposure descriptor, printing no status."""
    return f'project_secrets_register_exposed_source {kind} "{path}"\n'


def _register_and_preflight(kind, path):
    """Register one descriptor then run the exposure preflight."""
    return (
        _register_only(kind, path)
        + 'printf \'reg|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
    )


def test_descriptor_registry_semantics(tmp_path):
    """Prove the descriptor registry contract: only the three exact kinds
    are accepted, a missing optional path stays unregistered, deduplication
    applies only to an exact (kind, normalized lexical) pair, and a
    different kind or a lexical alias of the same physical location is
    retained.

    Keying deduplication on physical identity would let a no-follow
    registration silently suppress the dereference traversal required for
    the same location, so retention must use the descriptor's own
    identity."""
    home, launch = _present_pair(tmp_path)
    work = home / "work dir"
    work.mkdir()
    alias = home / "work alias"
    alias.symlink_to(work)
    body = (
        _preflight_prologue()
        + _register_only("tree-no-follow", work)
        + 'printf \'r-work|%s\\n\' "$?"\n'
        + _register_only("tree-no-follow", home / "nope")
        + 'printf \'r-missing|%s\\n\' "$?"\n'
        + _register_only("tree-no-follow", f"{work}/")
        + 'printf \'r-slash|%s\\n\' "$?"\n'
        + _register_only("tree-no-follow", f"{work}/./sub/..")
        + 'printf \'r-dots|%s\\n\' "$?"\n'
        + _register_only("tree-dereference", work)
        + 'printf \'r-kind|%s\\n\' "$?"\n'
        + _register_only("tree-no-follow", alias)
        + 'printf \'r-alias|%s\\n\' "$?"\n'
        + _register_only("no-follow", work)
        + 'printf \'r-badkind|%s\\n\' "$?"\n'
        + _register_only("file", work)
        + 'printf \'r-badtype|%s\\n\' "$?"\n'
        + 'printf \'count|%s\\n\' "$_PROJECT_SECRETS_DESCRIPTOR_COUNT"\n'
    )
    result = run_library(body, [launch, home])
    steps = _steps(result)
    assert steps["r-work"] == "0"
    assert steps["r-missing"] == "0"
    assert steps["r-slash"] == "0"
    assert steps["r-dots"] == "0"
    assert steps["r-kind"] == "0"
    assert steps["r-alias"] == "0"
    assert steps["r-badkind"] != "0"
    assert steps["r-badtype"] != "0"
    # Root scan + workspace + dereference twin + lexical alias: four.
    assert steps["count"] == "4"


def test_exposure_error_precedes_runtime_and_content(tmp_path):
    """Prove an exposure failure outranks all runtime and content work:
    with a hard-linked source inside an exposed tree and an available
    runtime, the preflight fails and neither resolution nor the version
    process ever runs.

    The ordering is the security boundary: an exposure collision must be
    reported before any later stage could observe, relay, or act on secret
    material."""
    home, launch = _present_pair(tmp_path)
    work = home / "work"
    work.mkdir()
    os.link(home / ".megali" / "secrets.env", work / "leak")
    log = tmp_path / "msb.log"
    msb_dir = tmp_path / "msb dir"
    _msb_shim(msb_dir, log, output=b"msb 0.0.1\n")
    body = (
        _preflight_prologue()
        + _register_only("tree-no-follow", work)
        + 'printf \'reg|%s\\n\' "$?"\n'
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, f"{msb_dir}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["reg"] == "0"
    assert steps["pin"] == "0"
    assert steps["preflight"] != "0"
    assert steps["resolve"] != "0"
    assert steps["check"] != "0"
    assert "exposure-alias" in result.stderr
    assert not log.exists()


def test_sources_inside_projects_root_reject_lexically_and_physically(tmp_path):
    """Prove present-pair sources are rejected from the projects root by
    both path forms: a secret directory whose normalized lexical path lies
    inside an explicit projects root, and one whose physical path resolves
    into the root through a symlink while its lexical path stays outside.

    Either form of containment would let project-controlled content reach
    the secret sources, so both spellings must fail closed without any
    descriptor registration."""
    body = (
        'project_secrets_discover "$1" "$2" explicit "$3"\n'
        'printf \'discover|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
    )

    # Lexical containment: the home, and with it the secret directory,
    # lives inside an explicit projects root.
    root = tmp_path / "lex case"
    home = root / "homedir"
    launch = root / "megali"
    launch.mkdir(parents=True)
    _make_pair(home / ".megali")
    result = run_library(body, [launch, home, root])
    steps = _steps(result)
    assert steps["discover"] == "0"
    assert steps["preflight"] != "0"
    assert "exposure-overlap" in result.stderr

    # Physical containment: the lexical secret directory is a symlink
    # into the projects root.
    home2 = tmp_path / "physical"
    store = home2 / "Projects" / "store"
    launch2 = home2 / "Projects" / "megali"
    launch2.mkdir(parents=True)
    _make_pair(store)
    (home2 / ".megali").symlink_to(store)
    result = run_library(body, [launch2, home2, home2 / "Projects"])
    steps = _steps(result)
    assert steps["discover"] == "0"
    assert steps["preflight"] != "0"
    assert "exposure-overlap" in result.stderr


def test_source_directory_overlap_rejects_both_directions_for_all_descriptors(tmp_path):
    """Prove every descriptor kind rejects source-directory overlap in both
    directions — a descriptor inside the secret directory and a descriptor
    tree containing it — for no-follow trees, dereference trees, and file
    descriptors (file inside the source directory and file equal to a
    source).

    One-sided containment checks would miss the ancestor case, where an
    exposed tree snapshot would copy the entire secret directory."""
    for kind in ("tree-no-follow", "tree-dereference"):
        home, launch = _present_pair(tmp_path / f"{kind}-below")
        inner = home / ".megali" / "inner"
        inner.mkdir()
        result = run_library(
            _preflight_prologue() + _register_and_preflight(kind, inner),
            [launch, home],
        )
        steps = _steps(result)
        assert steps["reg"] == "0", kind
        assert steps["preflight"] != "0", kind

        home, launch = _present_pair(tmp_path / f"{kind}-above")
        result = run_library(
            _preflight_prologue() + _register_and_preflight(kind, home),
            [launch, home],
        )
        steps = _steps(result)
        assert steps["reg"] == "0", kind
        assert steps["preflight"] != "0", kind

    home, launch = _present_pair(tmp_path / "file-below")
    note = home / ".megali" / "note.txt"
    note.write_text("x\n")
    result = run_library(
        _preflight_prologue() + _register_and_preflight("file", note),
        [launch, home],
    )
    assert _steps(result)["preflight"] != "0"

    home, launch = _present_pair(tmp_path / "file-equal")
    result = run_library(
        _preflight_prologue()
        + _register_and_preflight("file", home / ".megali" / "secrets.env"),
        [launch, home],
    )
    assert _steps(result)["preflight"] != "0"

    # Control: an unrelated descriptor of every kind passes.
    home, launch = _present_pair(tmp_path / "control")
    outside = tmp_path / "control outside"
    outside.mkdir()
    (outside / "plain").write_text("x\n")
    for kind, path in (
        ("tree-no-follow", outside),
        ("tree-dereference", outside),
        ("file", outside / "plain"),
    ):
        result = run_library(
            _preflight_prologue() + _register_and_preflight(kind, path),
            [launch, home],
        )
        assert _steps(result)["preflight"] == "0", kind


def test_projects_root_hard_link_alias_rejects(tmp_path):
    """Prove a hard link to a present-pair source anywhere under the
    projects root — here inside an unlaunched sibling project — fails the
    exposure preflight even when no descriptor was registered at all.

    The projects root is the project-controlled domain, so an alias there
    means a project could read the secret source without any exposed tree;
    the root itself must always be scanned."""
    home, launch = _present_pair(tmp_path)
    sibling = home / "Projects" / "other"
    sibling.mkdir()
    os.link(home / ".megali" / "secrets.env", sibling / "alias.env")
    body = (
        'project_secrets_discover "$1" "$2" default ""\n'
        'printf \'discover|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home])
    steps = _steps(result)
    assert steps["preflight"] != "0"
    assert "exposure-alias" in result.stderr

    # Control: without the alias the same launch passes.
    (sibling / "alias.env").unlink()
    result = run_library(body, [launch, home])
    assert _steps(result)["preflight"] == "0"


def test_each_descriptor_kind_rejects_source_aliases(tmp_path):
    """Prove each descriptor kind rejects a hard-link alias of a source
    file: an alias inside a no-follow tree, inside a dereference tree, and
    as the file descriptor itself.

    Hard links share the inode, so lexical and symlink checks cannot find
    them; every descriptor graph must compare filesystem identity."""
    for kind in ("tree-no-follow", "tree-dereference"):
        home, launch = _present_pair(tmp_path / f"{kind}-alias")
        tree = tmp_path / f"{kind}-tree"
        tree.mkdir()
        os.link(home / ".megali" / "secrets.env", tree / "linked")
        body = _preflight_prologue() + _register_and_preflight(kind, tree)
        result = run_library(body, [launch, home])
        steps = _steps(result)
        assert steps["reg"] == "0", kind
        assert steps["preflight"] != "0", kind
        assert "exposure-alias" in result.stderr, kind

        # Control: the same tree without the alias passes.
        (tree / "linked").unlink()
        result = run_library(body, [launch, home])
        assert _steps(result)["preflight"] == "0", kind

    home, launch = _present_pair(tmp_path / "file-alias")
    cred = tmp_path / "credentials"
    os.link(home / ".megali" / "secrets.yaml", cred)
    result = run_library(
        _preflight_prologue() + _register_and_preflight("file", cred),
        [launch, home],
    )
    steps = _steps(result)
    assert steps["preflight"] != "0"
    assert "exposure-alias" in result.stderr


def test_dereference_tree_rejects_nested_overlap_and_bad_graphs(tmp_path):
    """Prove dereference traversal follows links and rejects nested targets
    that overlap the secret directory or its sources, plus the bad graphs —
    dangling links, directory cycles, unreadable entries — while a clean
    graph with an unrelated symlink passes.

    Dereference trees describe content that is later copied with links
    followed, so every followed target must stay inside the trusted world
    and remain fully inspectable; any uncertainty fails closed instead of
    copying blindly. A walker without cycle detection would loop forever
    and hit the harness timeout."""
    def _case(name, setup):
        home, launch = _present_pair(tmp_path / name)
        secret = home / ".megali"
        (secret / "extra.txt").write_text("note\n")
        cfg = tmp_path / f"{name}-cfg"
        cfg.mkdir()
        setup(cfg, secret)
        result = run_library(
            _preflight_prologue() + _register_and_preflight("tree-dereference", cfg),
            [launch, home],
            timeout=30,
        )
        steps = _steps(result)
        assert steps["reg"] == "0", name
        assert steps["preflight"] != "0", name
        return result

    result = _case("dir-link", lambda cfg, secret: (cfg / "l").symlink_to(secret))
    assert "exposure-overlap" in result.stderr
    _case(
        "inner-link",
        lambda cfg, secret: (cfg / "l").symlink_to(secret / "extra.txt"),
    )
    result = _case(
        "values-link",
        lambda cfg, secret: (cfg / "l").symlink_to(secret / "secrets.env"),
    )
    assert "exposure-alias" in result.stderr
    result = _case(
        "dangling", lambda cfg, secret: (cfg / "l").symlink_to(cfg / "missing")
    )
    assert "exposure-dangling-link" in result.stderr
    result = _case("cycle", lambda cfg, secret: (cfg / "l").symlink_to(cfg))
    assert "exposure-graph-cycle" in result.stderr

    def _unreadable(cfg, secret):
        (cfg / "p").mkdir()
        (cfg / "p").chmod(0o000)

    result = _case("unreadable", _unreadable)
    assert "exposure-unreadable-entry" in result.stderr
    # Restore the mode so pytest's unprivileged temp-tree cleanup can remove
    # the unreadable directory; otherwise rm_rf warns and leaves garbage.
    for cand in tmp_path.glob("*-cfg/unreadable-cfg/p"):
        cand.chmod(0o755)

    # Control: a clean graph with a symlink to an unrelated target passes.
    home, launch = _present_pair(tmp_path / "clean")
    cfg = tmp_path / "clean-cfg"
    (cfg / "sub").mkdir(parents=True)
    (cfg / "sub" / "file").write_text("x\n")
    outside = tmp_path / "clean outside"
    outside.mkdir()
    (outside / "target").write_text("y\n")
    (cfg / "sub" / "ok-link").symlink_to(outside / "target")
    result = run_library(
        _preflight_prologue() + _register_and_preflight("tree-dereference", cfg),
        [launch, home],
        timeout=30,
    )
    assert _steps(result)["preflight"] == "0"


def test_case_alias_and_identity_uncertainty_fail_closed(tmp_path):
    """Prove identity comparisons resolve through the filesystem rather
    than path spelling — a case-only alias of the secret directory is
    rejected where the filesystem is case-insensitive — and that any
    inability to inspect a registered graph (a deleted descriptor, an
    unreadable tree) fails the preflight closed.

    String-only path checks would accept a case alias the filesystem
    treats as the same directory, and silently skipping uninspectable
    entries would let a hidden alias evade the scan."""
    # A registered file descriptor deleted before preflight cannot be
    # identity-checked, so the launch fails.
    home, launch = _present_pair(tmp_path / "vanishing")
    cred = tmp_path / "vanishing cred"
    cred.write_text("x\n")
    body = (
        _preflight_prologue()
        + _register_only("file", cred)
        + 'printf \'reg|%s\\n\' "$?"\n'
        + f'rm -f "{cred}"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home])
    steps = _steps(result)
    assert steps["reg"] == "0"
    assert steps["preflight"] != "0"
    assert "exposure-identity-uncertain" in result.stderr

    # An unreadable directory inside a no-follow tree cannot be scanned.
    home, launch = _present_pair(tmp_path / "opaque")
    work = tmp_path / "opaque work"
    (work / "p").mkdir(parents=True)
    (work / "p").chmod(0o000)
    result = run_library(
        _preflight_prologue() + _register_and_preflight("tree-no-follow", work),
        [launch, home],
    )
    steps = _steps(result)
    assert steps["preflight"] != "0"
    assert "exposure-unreadable-entry" in result.stderr
    # Restore the mode so pytest's unprivileged temp cleanup can remove it.
    (work / "p").chmod(0o755)

    # Case alias: only exercisable where the filesystem is
    # case-insensitive; otherwise this comparison is untestable here.
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "CaseFile").write_text("x\n")
    if not (probe / "casefile").exists():
        pytest.skip("filesystem is case-sensitive; case alias unexercisable")
    home, launch = _present_pair(tmp_path / "case", project="Megali")
    result = run_library(
        _preflight_prologue() + _register_and_preflight("tree-no-follow", home / ".MEGALI"),
        [launch, home],
    )
    assert _steps(result)["preflight"] != "0"


def test_descriptor_alias_keeps_strict_dereference_semantics(tmp_path):
    """Prove a dereference descriptor registered through a lexical alias of
    an already-registered no-follow tree keeps strict link-following
    semantics: the same physical location is traversed again with nested
    symlinks followed, and the hidden source alias is rejected.

    If the registry deduplicated by physical identity, the no-follow
    registration would suppress exactly the dereference traversal the
    snapshot copy needs, and the symlinked source would be copied."""
    home, launch = _present_pair(tmp_path)
    work = tmp_path / "work"
    (work / "cfg").mkdir(parents=True)
    (work / "cfg" / "leak").symlink_to(home / ".megali" / "secrets.env")
    body = (
        _preflight_prologue()
        + _register_only("tree-no-follow", work)
        + 'printf \'reg-nofollow|%s\\n\' "$?"\n'
        + _register_only("tree-dereference", work / "cfg")
        + 'printf \'reg-deref|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home])
    steps = _steps(result)
    assert steps["reg-nofollow"] == "0"
    assert steps["reg-deref"] == "0"
    assert steps["preflight"] != "0"
    assert "exposure-alias" in result.stderr

    # Control: the no-follow tree alone does not follow its nested link,
    # so the same layout passes without the dereference descriptor.
    body = (
        _preflight_prologue()
        + _register_only("tree-no-follow", work)
        + 'printf \'reg-nofollow|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home])
    assert _steps(result)["preflight"] == "0"


def test_runtime_version_process_contract(tmp_path):
    """Prove the exact runtime version process contract: one successful
    ``--version`` invocation of the pinned absolute executable with empty
    stderr and stdout exactly ``msb MAJOR.MINOR.PATCH`` plus zero or one
    trailing LF, decimal components without leading zeros, and a version in
    [0.6.12, 1.0.0).

    Every deviation — wrong range, trailing blank line, CRLF, doubled or
    missing separators, extra or missing text, leading zeros, prerelease or
    build suffixes, nonzero exit, stderr output — must fail closed rather
    than guess compatibility."""
    home, launch = _present_pair(tmp_path)
    good_outputs = [
        b"msb 0.6.12\n",
        b"msb 0.6.12",
        b"msb 0.6.13\n",
        b"msb 0.7.0\n",
        b"msb 0.9.99\n",
    ]
    bad_outputs = [
        b"msb 0.6.11\n",
        b"msb 1.0.0\n",
        b"msb 2.3.4\n",
        b"msb 0.6.12\n\n",
        b"msb 0.6.12\r\n",
        b"msb  0.6.12\n",
        b"msb 0.6.12 extra\n",
        b"msb 00.6.12\n",
        b"msb 0.06.12\n",
        b"msb 0.6.012\n",
        b"msb 0.6.12-rc1\n",
        b"msb 0.6.12+build\n",
        b"msb 0.6\n",
        b"msb 0.6.12.1\n",
        b"MSB 0.6.12\n",
        b" msb 0.6.12\n",
        b"msb\t0.6.12\n",
        b"",
        # Oversized output: the stdout read is bounded, and anything at or
        # beyond the bound rejects as malformed.
        b"msb 0.6.12\n" + b"x" * 5000,
    ]
    body_template = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
    )

    def _check(index, output, status=0, stderr=b""):
        case = tmp_path / "cases" / f"{index:02d}"
        log = case / "msb.log"
        msb = _msb_shim(case / "bin", log, output=output, status=status, stderr=stderr)
        result = run_library(
            body_template, [launch, home, f"{case / 'bin'}:{_SYSTEM_PATH}"]
        )
        steps = _steps(result)
        assert steps["pin"] == "0", output
        assert steps["preflight"] == "0", output
        assert steps["resolve"] == "0", output
        # Exactly one version process, through the pinned absolute path.
        assert log.read_text().splitlines() == [f"{msb} --version"], output
        return result, steps

    index = 0
    for output in good_outputs:
        _, steps = _check(index, output)
        assert steps["check"] == "0", output
        index += 1
    for output in bad_outputs:
        _, steps = _check(index, output)
        assert steps["check"] != "0", output
        index += 1
    result, steps = _check(index, b"msb 0.6.12\n", status=1)
    assert steps["check"] != "0"
    index += 1
    result, steps = _check(index, b"msb 0.6.12\n", stderr=b"warning\n")
    assert steps["check"] != "0"

    # Spot-check the two distinct failure classes.
    result, steps = _check(index + 1, b"msb 0.6.11\n")
    assert "incompatible-runtime" in result.stderr
    result, steps = _check(index + 2, b"msb 0.6.12 extra\n")
    assert "malformed-runtime-version" in result.stderr


def test_project_controlled_runtime_and_all_descriptor_aliases_reject(tmp_path):
    """Prove the resolved runtime is rejected from every project-controlled
    or exposed domain: inside the projects root, inside no-follow and
    dereference trees, equal to a file descriptor, and hard-linked into the
    projects root or a registered tree — while a runtime outside every
    domain resolves.

    A project-placed executable would let project content run before or
    instead of the trusted runtime, so location and identity are vetted
    before the version process is ever invoked."""
    log = tmp_path / "msb.log"

    def _resolve_body(msb_dir, registrations=""):
        return (
            _preflight_prologue()
            + registrations
            + 'project_secrets_pin_helpers "$3"\n'
            + 'printf \'pin|%s\\n\' "$?"\n'
            + "project_secrets_preflight_exposure\n"
            + 'printf \'preflight|%s\\n\' "$?"\n'
            + 'project_secrets_resolve_runtime "' + str(msb_dir) + ':$3"\n'
            + 'printf \'resolve|%s\\n\' "$?"\n'
        )

    def _expect_reject(name, msb_dir, registrations=""):
        home, launch = _present_pair(tmp_path / name)
        result = run_library(_resolve_body(msb_dir, registrations), [launch, home, _SYSTEM_PATH])
        steps = _steps(result)
        assert steps["pin"] == "0", name
        assert steps["preflight"] == "0", name
        assert steps["resolve"] != "0", name
        assert "runtime-exposure" in result.stderr, name

    # Runtime executable inside the projects root.
    root_case = tmp_path / "s-root-check"
    tools = root_case / "home" / "Projects" / "tools"
    _msb_shim(tools, log)
    _expect_reject("s-root-check", tools)

    # Inside a no-follow workspace tree.
    work = tmp_path / "s-work-tree"
    _msb_shim(work / "bin", log)
    _expect_reject("s-work", work / "bin", _register_only("tree-no-follow", work / "bin"))

    # Inside a dereference tree.
    cfg = tmp_path / "s-cfg-tree"
    _msb_shim(cfg / "bin", log)
    _expect_reject("s-cfg", cfg / "bin", _register_only("tree-dereference", cfg / "bin"))

    # Equal to a registered file descriptor.
    file_dir = tmp_path / "s-file-bin"
    msb = _msb_shim(file_dir, log)
    _expect_reject("s-file", file_dir, _register_only("file", msb))

    # Hard-linked into the projects root.
    link_dir = tmp_path / "s-link-bin"
    msb = _msb_shim(link_dir, log)
    home, launch = _present_pair(tmp_path / "s-link")
    os.link(msb, home / "Projects" / "msb-alias")
    result = run_library(_resolve_body(link_dir), [launch, home, _SYSTEM_PATH])
    assert _steps(result)["resolve"] != "0"
    assert "runtime-exposure" in result.stderr

    # Hard-linked under a registered tree.
    tree = tmp_path / "s-link-tree-alias"
    tree.mkdir()
    os.link(msb, tree / "alias")
    _expect_reject("s-link-tree", link_dir, _register_only("tree-no-follow", tree))

    # Control: a runtime outside every domain resolves.
    home, launch = _present_pair(tmp_path / "s-ok")
    clean_tree = tmp_path / "s-ok outside"
    clean_tree.mkdir()
    (clean_tree / "plain").write_text("x\n")
    msb_dir = tmp_path / "s-ok-bin"
    ok_msb = _msb_shim(msb_dir, log)
    result = run_library(
        _resolve_body(msb_dir, _register_only("tree-no-follow", clean_tree)),
        [launch, home, _SYSTEM_PATH],
    )
    steps = _steps(result)
    assert steps["resolve"] == "0"
    # None of the rejected scenarios ever executed a runtime.
    assert not log.exists()


def test_runtime_identity_is_absolute_and_pinned(tmp_path):
    """Prove the resolved runtime is stored as an absolute pinned path with
    filesystem identity: a later PATH change cannot redirect it, replacing
    the executable at the stored path fails identity revalidation before
    any further version process, and revalidation before resolution fails.

    PATH-based lookup after the pin would let an environment file swap in
    a different runtime between the compatibility check and the launch."""
    home, launch = _present_pair(tmp_path)
    log_a = tmp_path / "a.log"
    log_b = tmp_path / "b.log"
    dir_a = tmp_path / "msb a"
    dir_b = tmp_path / "msb b"
    msb_a = _msb_shim(dir_a, log_a, output=b"msb 0.6.12\n")
    _msb_shim(dir_b, log_b, output=b"msb 0.9.5\n")
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$4"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_revalidate_runtime\n"
        + 'printf \'revalidate|%s\\n\' "$?"\n'
        + 'printf \'path|%s\\n\' "$PROJECT_SECRETS_MSB_PATH"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$5"\n'
        + 'printf \'resolve2|%s\\n\' "$?"\n'
        + 'printf \'path2|%s\\n\' "$PROJECT_SECRETS_MSB_PATH"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check2|%s\\n\' "$?"\n'
    )
    args = [launch, home, _SYSTEM_PATH, f"{dir_a}:{_SYSTEM_PATH}", f"{dir_b}:{_SYSTEM_PATH}"]
    result = run_library(body, args)
    steps = _steps(result)
    assert steps["resolve"] == "0"
    assert steps["revalidate"] == "0"
    assert os.path.isabs(steps["path"])
    assert steps["path"] == str(msb_a)
    assert steps["check"] == "0"
    # PATH change cannot redirect: the stored path and executable stay.
    assert steps["resolve2"] == "0"
    assert steps["path2"] == str(msb_a)
    assert steps["check2"] == "0"
    assert log_a.read_text().splitlines() == [f"{msb_a} --version"] * 2
    assert not log_b.exists()

    # Replacement at the stored path fails identity revalidation, and the
    # version check runs no further process.
    replacement = tmp_path / "replacement"
    replacement.write_text("#!/bin/bash\nexit 1\n")
    replacement.chmod(0o755)
    invocations_before = len(log_a.read_text().splitlines())
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$4"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
        + f'mv "{replacement}" "{msb_a}"\n'
        + "project_secrets_revalidate_runtime\n"
        + 'printf \'revalidate|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check2|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, _SYSTEM_PATH, f"{dir_a}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["preflight"] == "0"
    assert steps["resolve"] == "0"
    assert steps["check"] == "0"
    assert steps["revalidate"] != "0"
    assert steps["check2"] != "0"
    assert "runtime-identity" in result.stderr
    # Exactly one more version process ran: the post-replacement check
    # failed at identity revalidation before invoking the executable.
    assert len(log_a.read_text().splitlines()) == invocations_before + 1

    # Revalidation before resolution has nothing pinned and fails.
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + "project_secrets_preflight_exposure\n"
        + "project_secrets_revalidate_runtime\n"
        + 'printf \'early-revalidate|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, _SYSTEM_PATH])
    assert _steps(result)["early-revalidate"] != "0"


def test_helper_pin_contract(tmp_path):
    """Prove helper pinning: the five allowed helpers resolve once through
    the initial PATH into absolute readonly paths with pinned identity;
    later calls revalidate the stored identities instead of re-resolving,
    so a PATH change cannot redirect them and a replaced helper is
    rejected; and helpers located inside or hard-linked into the projects
    root or a descriptor never pin and never execute.

    Helper shadows under project control would let project content observe
    or alter the security decisions these tools participate in."""
    home, launch = _present_pair(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    shim_log = tmp_path / "helper.log"
    shim_dir = tmp_path / "shim dir"
    _helper_shims(shim_dir, shim_log)
    msb_log = tmp_path / "msb.log"
    msb_dir = tmp_path / "msb dir"
    _msb_shim(msb_dir, msb_log)
    pin_body = (
        _preflight_prologue()
        + _register_only("tree-no-follow", work)
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + 'printf \'pin-tr|%s\\n\' "$PROJECT_SECRETS_HELPER_TR"\n'
        + 'printf \'pin-cmp|%s\\n\' "$PROJECT_SECRETS_HELPER_CMP"\n'
        + 'printf \'pin-mktemp|%s\\n\' "$PROJECT_SECRETS_HELPER_MKTEMP"\n'
        + 'printf \'pin-chmod|%s\\n\' "$PROJECT_SECRETS_HELPER_CHMOD"\n'
        + 'printf \'pin-rm|%s\\n\' "$PROJECT_SECRETS_HELPER_RM"\n'
        + 'project_secrets_pin_helpers "$4"\n'
        + 'printf \'repin|%s\\n\' "$?"\n'
        + '( PROJECT_SECRETS_HELPER_TR=/tmp/nope ) 2>/dev/null\n'
        + 'printf \'readonly|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
    )
    result = run_library(
        pin_body,
        [launch, home, f"{msb_dir}:{shim_dir}:{_SYSTEM_PATH}", _SYSTEM_PATH],
    )
    steps = _steps(result)
    assert steps["pin"] == "0"
    for tool in _HELPER_TOOLS:
        assert steps[f"pin-{tool}"] == str(shim_dir / tool), tool
        assert os.path.isabs(steps[f"pin-{tool}"]), tool
    assert steps["repin"] == "0"
    assert steps["readonly"] != "0"
    assert steps["preflight"] == "0"
    assert steps["resolve"] == "0"
    assert steps["check"] == "0"
    # The library's own tool use goes through the pinned helpers.
    logged = shim_log.read_text().splitlines()
    assert any(line.startswith("mktemp") for line in logged)
    assert any(line.startswith("rm") for line in logged)

    # A replaced helper fails identity revalidation on the next call.
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin1|%s\\n\' "$?"\n'
        + f'printf \'changed\\n\' > "{shim_dir}/tr.new"\n'
        + f'mv "{shim_dir}/tr.new" "{shim_dir}/tr"\n'
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin2|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, f"{shim_dir}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["pin1"] == "0"
    assert steps["pin2"] != "0"
    assert "helper-identity" in result.stderr

    # Helpers inside the projects root never pin and never execute.
    evil_home, evil_launch = _present_pair(tmp_path / "evil")
    evil_log = tmp_path / "evil.log"
    evil_dir = evil_home / "Projects" / "tools"
    for tool in _HELPER_TOOLS:
        _shim(
            evil_dir,
            tool,
            "#!/bin/bash\n" + f'printf \'%s\\n\' "$0" >> "{evil_log}"\n',
        )
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
    )
    result = run_library(body, [evil_launch, evil_home, f"{evil_dir}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["pin"] != "0"
    assert "helper-exposure" in result.stderr
    assert not evil_log.exists()

    # A helper hard-linked under a registered tree never pins.
    link_home, link_launch = _present_pair(tmp_path / "hardlink")
    link_shims = _helper_shims(tmp_path / "link shims", tmp_path / "link.log")
    link_tree = tmp_path / "link tree"
    link_tree.mkdir()
    os.link(link_shims["tr"], link_tree / "tr-alias")
    body = (
        _preflight_prologue()
        + _register_only("tree-no-follow", link_tree)
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
    )
    result = run_library(
        body, [link_launch, link_home, f"{tmp_path / 'link shims'}:{_SYSTEM_PATH}"]
    )
    steps = _steps(result)
    assert steps["pin"] != "0"
    assert "helper-exposure" in result.stderr


def test_external_resolution_ignores_type_function_shadow():
    """Prove external resolution cannot be shadowed by a shell function
    named ``type`` defined before the resolver runs: the resolver must
    invoke the Bash ``type`` builtin directly, so a harness or environment
    that defines ``type(){ ...; }`` cannot redirect helper or runtime
    lookup or decide what resolves.

    ``type -P`` without the ``builtin`` prefix consults the function table
    first, so a shadow would control both which names resolve and what
    they resolve to — exactly the redirection the PATH-only resolution
    contract exists to prevent."""
    shadow = 'type() { printf "/shadowed-%s\\n" "$1"; return 0; }\n'
    body = (
        shadow
        + 'found="$(_project_secrets_find_external tr "$1")" || exit 20\n'
        + '[ "$found" = "$2" ] || exit 21\n'
        + '_project_secrets_find_external no-such-project-secrets-tool "$1" \
'
        + "&& exit 22\n"
        + "exit 0\n"
    )
    result = run_library(body, [_SYSTEM_PATH, shutil.which("tr")])
    assert result.returncode == 0, result.stderr


def test_preflight_after_registry_before_runtime(tmp_path):
    """Prove the ordering gates: the runtime cannot be resolved before a
    successful exposure preflight, and a descriptor registered after a
    successful preflight invalidates it, forcing a new preflight before
    the runtime is trusted again.

    Without these gates a caller mistake could resolve the runtime against
    a stale registry that no longer describes what will actually be
    exposed, and the version process could run before the collision is
    known."""
    def _scenario(log_name):
        log = tmp_path / log_name
        msb_dir = tmp_path / f"{log_name}-bin"
        _msb_shim(msb_dir, log)
        return log, msb_dir

    # Runtime before preflight: rejected, and no version process runs.
    log, msb_dir = _scenario("order-a.log")
    home, launch = _present_pair(tmp_path / "order-a")
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'early-resolve|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, f"{msb_dir}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["early-resolve"] != "0"
    assert "exposure-not-preflighted" in result.stderr
    assert steps["preflight"] == "0"
    assert steps["resolve"] == "0"
    assert steps["check"] == "0"
    assert log.read_text().splitlines() == [f"{msb_dir / 'msb'} --version"]

    # A descriptor registered after a successful preflight invalidates it.
    log, msb_dir = _scenario("order-b.log")
    home, launch = _present_pair(tmp_path / "order-b")
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
        + _register_only("tree-no-follow", home)
        + 'printf \'reg|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve2|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check2|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight2|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, f"{msb_dir}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["preflight"] == "0"
    assert steps["resolve"] == "0"
    assert steps["check"] == "0"
    assert steps["reg"] == "0"
    assert steps["resolve2"] != "0"
    assert steps["check2"] != "0"
    assert steps["preflight2"] != "0"
    # Still exactly one version process: the gated calls never ran one.
    assert log.read_text().splitlines() == [f"{msb_dir / 'msb'} --version"]


def test_replaced_helper_fails_identity_before_use(tmp_path):
    """Prove every helper invocation revalidates the pinned identity
    immediately before use: replacing the pinned ``mktemp`` or ``rm``
    shim after pinning makes check_runtime fail with helper-identity and
    the replacement executable is never invoked, so no version process
    runs either.

    Pinning without per-use revalidation would leave a window in which a
    swapped helper — one the launcher never vetted — creates or removes
    the version check's staging files."""
    home, launch = _present_pair(tmp_path)
    msb_log = tmp_path / "msb.log"
    msb_dir = tmp_path / "msb dir"
    _msb_shim(msb_dir, msb_log)
    shim_log = tmp_path / "helper.log"
    shim_dir = tmp_path / "shim dir"
    _helper_shims(shim_dir, shim_log)
    replacement_log = tmp_path / "replacement.log"

    def _replacement(name):
        """A fresh, invocation-logging shim for one helper tool."""
        return _shim(
            tmp_path / "replacement dir",
            name,
            "#!/bin/bash\n"
            f'printf \'%s\\n\' "$0" >> "{replacement_log}"\n'
            f'exec "{shutil.which(name)}" "$@"\n',
        )

    def _check_with_replacement(name):
        body = (
            _preflight_prologue()
            + 'project_secrets_pin_helpers "$3"\n'
            + 'printf \'pin|%s\\n\' "$?"\n'
            + "project_secrets_preflight_exposure\n"
            + 'printf \'preflight|%s\\n\' "$?"\n'
            + 'project_secrets_resolve_runtime "$3"\n'
            + 'printf \'resolve|%s\\n\' "$?"\n'
            + f'mv "{_replacement(name)}" "{shim_dir}/{name}"\n'
            + "project_secrets_check_runtime\n"
            + 'printf \'check|%s\\n\' "$?"\n'
        )
        return run_library(
            body, [launch, home, f"{msb_dir}:{shim_dir}:{_SYSTEM_PATH}"]
        )

    for name in ("mktemp", "rm"):
        result = _check_with_replacement(name)
        steps = _steps(result)
        assert steps["pin"] == "0", name
        assert steps["preflight"] == "0", name
        assert steps["resolve"] == "0", name
        assert steps["check"] != "0", name
        assert "helper-identity" in result.stderr, name
        # The replacement was never invoked and no version process ran.
        assert not replacement_log.exists(), name
        assert not msb_log.exists(), name


def test_rediscovery_invalidates_preflight_state(tmp_path):
    """Prove a rediscovery never inherits a stale exposure preflight:
    after discover/register/preflight/resolve succeed for one pair, a
    second discover clears the preflight marker, so resolve_runtime and
    check_runtime fail with exposure-not-preflighted until a new
    preflight runs against the rediscovered pair.

    A caller that rediscovers (for example on a retry) would otherwise
    trust an exposure set vetted for a different secret pair and run the
    runtime against sources the new pair never approved."""
    home, launch = _present_pair(tmp_path)
    home_b, launch_b = _present_pair(tmp_path, project="other")
    log = tmp_path / "msb.log"
    msb_dir = tmp_path / "msb dir"
    _msb_shim(msb_dir, log)
    path = f"{msb_dir}:{_SYSTEM_PATH}"
    body = (
        'project_secrets_discover "$1" "$2" default ""\n'
        'printf \'discover1|%s\\n\' "$?"\n'
        "project_secrets_register_projects_root_scan\n"
        'project_secrets_pin_helpers "$3"\n'
        'printf \'pin|%s\\n\' "$?"\n'
        "project_secrets_preflight_exposure\n"
        'printf \'preflight1|%s\\n\' "$?"\n'
        'project_secrets_resolve_runtime "$3"\n'
        'printf \'resolve1|%s\\n\' "$?"\n'
        "project_secrets_check_runtime\n"
        'printf \'check1|%s\\n\' "$?"\n'
        'project_secrets_discover "$4" "$2" default ""\n'
        'printf \'discover2|%s\\n\' "$?"\n'
        'printf \'pair|%s\\n\' "$PROJECT_SECRETS_PAIR_STATE"\n'
        'project_secrets_resolve_runtime "$3"\n'
        'printf \'resolve2|%s\\n\' "$?"\n'
        "project_secrets_check_runtime\n"
        'printf \'check2|%s\\n\' "$?"\n'
        "project_secrets_register_projects_root_scan\n"
        "project_secrets_preflight_exposure\n"
        'printf \'preflight2|%s\\n\' "$?"\n'
        'project_secrets_resolve_runtime "$3"\n'
        'printf \'resolve3|%s\\n\' "$?"\n'
        "project_secrets_check_runtime\n"
        'printf \'check3|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, path, launch_b])
    steps = _steps(result)
    assert steps["discover1"] == "0"
    assert steps["pin"] == "0"
    assert steps["preflight1"] == "0"
    assert steps["resolve1"] == "0"
    assert steps["check1"] == "0"
    assert steps["discover2"] == "0"
    assert steps["pair"] == "present"
    assert steps["resolve2"] != "0"
    assert steps["check2"] != "0"
    assert "exposure-not-preflighted" in result.stderr
    assert steps["preflight2"] == "0"
    assert steps["resolve3"] == "0"
    assert steps["check3"] == "0"
    # The pre-gate failure never ran an extra version process.
    assert log.read_text().splitlines() == [f"{msb_dir / 'msb'} --version"] * 2


def test_post_registration_hard_link_fails_revalidation(tmp_path):
    """Prove revalidation re-vets executable exposure against the current
    descriptor registry: a tree registered after pinning that contains a
    hard link to the pinned runtime, or to a pinned helper, makes
    revalidate_runtime/check_runtime and the helper gates fail with the
    exposure class even though the pinned inode identity is unchanged.

    In-place edits through a hard link preserve the inode, so identity
    revalidation alone would keep trusting a binary that project content
    can now rewrite through the exposed link; only re-running the
    executable-exposure rejection against the current registry closes
    that window."""
    base = tmp_path / "revet"
    home, launch = _present_pair(base)
    msb_log = base / "msb.log"
    msb_dir = base / "msb dir"
    _msb_shim(msb_dir, msb_log)
    shim_dir = base / "shim dir"
    _helper_shims(shim_dir, base / "helper.log")
    workspace = base / "workspace"
    workspace.mkdir()
    path = f"{msb_dir}:{shim_dir}:{_SYSTEM_PATH}"

    # Runtime: a hard link to the pinned runtime appears in a tree that
    # is registered only after resolution.
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight1|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve1|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check1|%s\\n\' "$?"\n'
        + 'ln "$PROJECT_SECRETS_MSB_PATH" "$4/msb-alias"\n'
        + 'project_secrets_register_exposed_source tree-no-follow "$4"\n'
        + 'printf \'reg|%s\\n\' "$?"\n'
        + "project_secrets_revalidate_runtime\n"
        + 'printf \'revalidate|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight2|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check2|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, path, workspace])
    steps = _steps(result)
    assert steps["preflight1"] == "0"
    assert steps["resolve1"] == "0"
    assert steps["check1"] == "0"
    assert steps["reg"] == "0"
    assert steps["revalidate"] != "0"
    assert "runtime-exposure" in result.stderr
    assert steps["preflight2"] == "0"
    assert steps["check2"] != "0"
    # No further version process ran after the link was exposed.
    assert msb_log.read_text().splitlines() == [f"{msb_dir / 'msb'} --version"]

    # Helper: the same window for a pinned helper, caught by the helper
    # re-run gate and by check_runtime's per-use helper gate.
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin1|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight1|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve1|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check1|%s\\n\' "$?"\n'
        + 'ln "$PROJECT_SECRETS_HELPER_MKTEMP" "$4/mktemp-alias"\n'
        + 'project_secrets_register_exposed_source tree-no-follow "$4"\n'
        + 'printf \'reg|%s\\n\' "$?"\n'
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'repin|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight2|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check2|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, path, workspace])
    steps = _steps(result)
    assert steps["pin1"] == "0"
    assert steps["preflight1"] == "0"
    assert steps["resolve1"] == "0"
    assert steps["check1"] == "0"
    assert steps["reg"] == "0"
    assert steps["repin"] != "0"
    assert "helper-exposure" in result.stderr
    assert steps["preflight2"] == "0"
    assert steps["check2"] != "0"
    assert "helper-exposure" in result.stderr
    # check2 failed at the helper gate, before any staging or version
    # process; only this scenario's own check1 ran the runtime.
    assert msb_log.read_text().splitlines() == [f"{msb_dir / 'msb'} --version"] * 2


def test_failing_helper_reports_helper_failed_not_identity(tmp_path):
    """Prove a helper that fails when run is reported as helper-failed,
    distinct from helper-identity: the identity gate passed because the
    pinned inode is intact, so blaming the identity would misreport an
    intact trust anchor and hide a plain runtime failure."""
    home, launch = _present_pair(tmp_path)
    msb_log = tmp_path / "msb.log"
    msb_dir = tmp_path / "msb dir"
    _msb_shim(msb_dir, msb_log)
    shim_dir = tmp_path / "shim dir"
    _helper_shims(shim_dir, tmp_path / "helper.log")
    # The pinned mktemp exists and is intact but fails when executed.
    _shim(shim_dir, "mktemp", "#!/bin/bash\nexit 1\n")
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
    )
    result = run_library(
        body, [launch, home, f"{msb_dir}:{shim_dir}:{_SYSTEM_PATH}"]
    )
    steps = _steps(result)
    assert steps["pin"] == "0"
    assert steps["preflight"] == "0"
    assert steps["resolve"] == "0"
    assert steps["check"] != "0"
    assert "helper-failed" in result.stderr
    assert "helper-identity" not in result.stderr
    # The version process never ran: staging failed first.
    assert not msb_log.exists()


def test_vanished_output_file_fails_cleanly(tmp_path):
    """Prove a version output file that vanishes between the version
    process and the read fails check_runtime cleanly: staging files are
    removed, the calling script keeps running, and the diagnostic is the
    library's own class instead of a raw shell redirection error.

    An unguarded `exec 66<file` on a vanished file leaves the failure to
    the shell's redirection machinery, leaking the temp path on stderr
    and skipping the library's staging cleanup."""
    home, launch = _present_pair(tmp_path)
    msb_dir = tmp_path / "msb dir"
    # The runtime prints a valid version and then removes its own stdout
    # file through /proc/self/fd, so the output file is gone before the
    # library reads it.
    _shim(
        msb_dir,
        "msb",
        "#!/bin/bash\n"
        "printf 'msb 0.6.12\\n'\n"
        "exec 9<&1\n"
        'rm -f "$(readlink -f /proc/self/fd/9)"\n'
        "exec 9<&-\n"
        "exit 0\n",
    )
    shim_dir = tmp_path / "shim dir"
    _helper_shims(shim_dir, tmp_path / "helper.log")
    staging = tmp_path / "staging"
    staging.mkdir()
    body = (
        _preflight_prologue()
        + 'project_secrets_pin_helpers "$3"\n'
        + "project_secrets_preflight_exposure\n"
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'export TMPDIR="$4"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
        + "shopt -s nullglob\n"
        + 'leftovers=( "$4"/* )\n'
        + 'printf \'leftover|%s\\n\' "${#leftovers[@]}"\n'
    )
    result = run_library(
        body, [launch, home, f"{msb_dir}:{shim_dir}:{_SYSTEM_PATH}", staging]
    )
    steps = _steps(result)
    assert steps["check"] != "0"
    assert "runtime-output-vanished" in result.stderr
    assert "No such file or directory" not in result.stderr
    assert steps["leftover"] == "0"


def test_no_pair_launch_cannot_reach_runtime_work(tmp_path):
    """Prove the no-pair contract: preflight on a none pair returns 0 but
    never marks exposure preflighted, so resolve_runtime and
    check_runtime stay impossible for a launch with no secrets even
    though helpers can be pinned.

    Treating a no-pair preflight as exposure-ok would let a launcher
    resolve and run the runtime for a launch whose registry was never
    vetted against any pair."""
    home = _make_home(tmp_path)
    launch = tmp_path / "outside"
    launch.mkdir()
    msb_log = tmp_path / "msb.log"
    msb_dir = tmp_path / "msb dir"
    _msb_shim(msb_dir, msb_log)
    body = (
        'project_secrets_discover "$1" "$2" default ""\n'
        + 'printf \'discover|%s\\n\' "$?"\n'
        + 'printf \'pair|%s\\n\' "$PROJECT_SECRETS_PAIR_STATE"\n'
        + "project_secrets_register_projects_root_scan\n"
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'preflight|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'resolve|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'check|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, f"{msb_dir}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["discover"] == "0"
    assert steps["pair"] == "none"
    assert steps["pin"] == "0"
    assert steps["preflight"] == "0"
    assert steps["resolve"] != "0"
    assert steps["check"] != "0"
    assert "exposure-not-preflighted" in result.stderr
    assert not msb_log.exists()


# ---------------------------------------------------------------------------
# Strict value and policy grammars.

def _policy(*secrets):
    """Build one valid policy document from (name, destinations,
    injections) triples; injections None omits the inject field (the
    headers default)."""
    lines = []
    for name, dests, injects in secrets:
        lines.append(f"{name}:")
        lines.append("  allow:")
        lines.extend(f"    - {dest}" for dest in dests)
        if injects is not None:
            lines.append("  inject:")
            lines.extend(f"    - {item}" for item in injects)
    return "\n".join(lines) + "\n"


# Discover a present pair and register the projects-root scan; the
# preflight and runtime steps below build on it.
_DISCOVER_GATE = (
    'project_secrets_discover "$1" "$2" default ""\n'
    "project_secrets_register_projects_root_scan\n"
)

# The discovery state the environment-source check consumes: a present
# pair with a preflighted exposure registry and no runtime work.
_PAIR_GATE = _DISCOVER_GATE + "project_secrets_preflight_exposure\n"

# The full gate metadata validation requires: discovery, the projects
# root scan, pinned helpers, a preflighted exposure registry, and an
# established compatible runtime, so content parsing is reachable only
# after the runtime version check has succeeded.
_META_GATE = (
    _DISCOVER_GATE
    + 'project_secrets_pin_helpers "$3"\n'
    + "project_secrets_preflight_exposure\n"
    + 'project_secrets_resolve_runtime "$3"\n'
    + "project_secrets_check_runtime\n"
)


def _meta_runtime_path(base):
    """A PATH whose first entry holds a compatible msb shim, satisfying
    the runtime half of the metadata gate as argument $3."""
    msb_dir = base / "msb dir"
    _msb_shim(msb_dir, base / "msb.log")
    return f"{msb_dir}:{_SYSTEM_PATH}"

_META_VALIDATE = (
    "project_secrets_validate_metadata\n"
    'printf \'meta|%s\\n\' "$?"\n'
)

_EFFECTIVE_BODY = (
    _META_VALIDATE
    + 'printf \'names|%s\\n\' "${PROJECT_SECRET_NAMES[*]}"\n'
    + 'printf \'allow0|%s\\n\' "${_PROJECT_SECRETS_POLICY_ALLOW[0]}"\n'
    + 'printf \'inject0|%s\\n\' "${_PROJECT_SECRETS_POLICY_INJECT[0]}"\n'
    + 'printf \'allow1|%s\\n\' "${_PROJECT_SECRETS_POLICY_ALLOW[1]}"\n'
    + 'printf \'inject1|%s\\n\' "${_PROJECT_SECRETS_POLICY_INJECT[1]}"\n'
    + 'printf \'allow2|%s\\n\' "${_PROJECT_SECRETS_POLICY_ALLOW[2]}"\n'
    + 'printf \'inject2|%s\\n\' "${_PROJECT_SECRETS_POLICY_INJECT[2]}"\n'
)


def _meta_run(base, env_text, yaml_text, body=_META_VALIDATE, preflight=True):
    """Run one fresh-Bash metadata scenario over a custom present pair.

    The pair text is written from the test so byte-exact sources (NUL,
    BOM, non-ASCII) stay under the test's control; the body runs after the
    full discover/scan/pin/preflight/resolve/check gate unless preflight
    is disabled, leaving only discovery state."""
    home = base / "home"
    launch = home / "Projects" / "megali"
    launch.mkdir(parents=True)
    _make_pair(home / ".megali", env_text, yaml_text)
    prologue = _META_GATE if preflight else _DISCOVER_GATE
    return run_library(prologue + body, [launch, home, _meta_runtime_path(base)])


def _meta_run_env_bytes(base, env_bytes, yaml_text, body=_META_VALIDATE):
    """One metadata scenario whose value source is written as raw bytes."""
    home = base / "home"
    launch = home / "Projects" / "megali"
    launch.mkdir(parents=True)
    secret = home / ".megali"
    secret.mkdir()
    (secret / "secrets.env").write_bytes(env_bytes)
    (secret / "secrets.yaml").write_text(yaml_text)
    return run_library(
        _META_GATE + body, [launch, home, _meta_runtime_path(base)]
    )


def _meta_run_yaml_bytes(base, env_text, yaml_bytes, body=_META_VALIDATE):
    """One metadata scenario whose policy source is written as raw
    bytes."""
    home = base / "home"
    launch = home / "Projects" / "megali"
    launch.mkdir(parents=True)
    secret = home / ".megali"
    secret.mkdir()
    (secret / "secrets.env").write_text(env_text)
    (secret / "secrets.yaml").write_bytes(yaml_bytes)
    return run_library(
        _META_GATE + body, [launch, home, _meta_runtime_path(base)]
    )


def test_metadata_rejects_before_compatible_preflight(tmp_path):
    """Prove metadata validation is unreachable without a successful
    present-pair exposure preflight: no preflight and a none pair both
    fail with the preflight class before any source byte is parsed, while
    a preflighted present pair validates.

    Content parsing before the exposure decision would let a launch
    observe secret material for a pair whose exposure was never vetted;
    the gate must fail closed before the first read, retaining nothing."""
    marker = "METASECRETMARKER"
    env = f"API_KEY={marker}value\n"
    policy = _policy(("API_KEY", ["api.example.com"], None))
    body = _META_VALIDATE + 'printf \'names|%s\\n\' "${PROJECT_SECRET_NAMES[*]}"\n'

    result = _meta_run(tmp_path / "noflight", env, policy, body, preflight=False)
    steps = _steps(result)
    assert steps["meta"] != "0"
    assert "exposure-not-preflighted" in result.stderr
    assert steps["names"] == ""
    assert marker not in result.stdout + result.stderr

    home = _make_home(tmp_path / "nonepair")
    (home / "Projects" / "megali").mkdir(parents=True)
    result = run_library(
        _META_GATE + _META_VALIDATE,
        [home / "Projects" / "megali", home, _meta_runtime_path(tmp_path / "nonepair")],
    )
    assert _steps(result)["meta"] != "0"
    assert "exposure-not-preflighted" in result.stderr

    result = _meta_run(tmp_path / "ok", env, policy, body)
    steps = _steps(result)
    assert steps["meta"] == "0"
    assert steps["names"] == "API_KEY"


def test_metadata_requires_runtime_check(tmp_path):
    """Prove runtime compatibility gates content parsing: with a present,
    exposure-preflighted pair and a resolved runtime whose version check
    has not succeeded, metadata validation fails with runtime-not-checked
    before any source byte is parsed, so malformed content is not even
    reached; a successful check lets parsing proceed and report the
    content error; and a rediscovery or a later registration clears the
    checked marker, re-requiring the check.

    The spec puts runtime incompatibility before empty or malformed
    content, so parsing before the check would report content errors for a
    runtime never proven compatible; a checked marker surviving
    rediscovery or a registry change would trust a check made against an
    earlier pair or a stale exposure set."""
    base = tmp_path / "case"
    home, launch = _present_pair(base)
    (home / ".megali" / "secrets.env").write_text("API_KEY=1\nMALFORMEDLINE\n")
    (home / ".megali" / "secrets.yaml").write_text(
        _policy(("API_KEY", ["api.example.com"], None))
    )
    path = _meta_runtime_path(base)
    outside = base / "outside"
    outside.mkdir()
    (outside / "plain").write_text("x\n")

    def _validate(label):
        return (
            "project_secrets_validate_metadata\n"
            f'printf \'{label}|%s\\n\' "$?"\n'
        )

    # Resolved but never checked: validation stops at the runtime gate
    # before the malformed content is parsed.
    body = (
        _DISCOVER_GATE
        + 'project_secrets_pin_helpers "$3"\n'
        + "project_secrets_preflight_exposure\n"
        + 'project_secrets_resolve_runtime "$3"\n'
        + _META_VALIDATE
    )
    result = run_library(body, [launch, home, path])
    steps = _steps(result)
    assert steps["meta"] != "0"
    assert "runtime-not-checked" in result.stderr
    assert "value-malformed-entry" not in result.stderr
    assert "exposure-not-preflighted" not in result.stderr

    # Checked: the content error is now reachable; a rediscovery clears
    # the checked marker; a re-check restores it; a later registration
    # clears it again.
    body = (
        _META_GATE
        + 'printf \'check1|%s\\n\' "$?"\n'
        + _validate("v-after-check")
        + _DISCOVER_GATE
        + "project_secrets_preflight_exposure\n"
        + 'project_secrets_resolve_runtime "$3"\n'
        + _validate("v-rediscovered")
        + "project_secrets_check_runtime\n"
        + _validate("v-rechecked")
        + _register_only("tree-no-follow", outside)
        + "project_secrets_preflight_exposure\n"
        + _validate("v-registered")
    )
    result = run_library(body, [launch, home, path])
    steps = _steps(result)
    assert steps["check1"] == "0"
    assert steps["v-after-check"] != "0"
    assert steps["v-rediscovered"] != "0"
    assert steps["v-rechecked"] != "0"
    assert steps["v-registered"] != "0"
    # Each of the four validations emits exactly one diagnostic; the
    # runtime gate fired exactly twice and the content parser exactly
    # twice, proving the parse happens only after a fresh check.
    assert result.stderr.count("runtime-not-checked") == 2
    assert result.stderr.count("value-malformed-entry") == 2


def test_metadata_and_functions_are_readonly_before_ordinary_config(tmp_path):
    """Prove a successful metadata validation freezes the library before
    trusted ordinary configuration can run: every library function is
    readonly so redefinition and unset fail, the name, policy, path,
    marker, descriptor-registry, and pin-bookkeeping state is readonly so
    reassignment and unset fail, and every further lifecycle call — a
    second validate_metadata, rediscovery, registration, preflight,
    runtime check, helper pinning, and runtime resolution — fails with
    the documented metadata-already-validated class and path only, with
    no raw shell readonly error and no secret content in the output.

    Trusted ordinary configuration runs between metadata validation and
    value retention; a replaceable function or mutable path, policy,
    descriptor, or pin state there could redirect which sources, names,
    and runtime the later value pass trusts — a zeroed descriptor count
    would even let a hard-linked executable escape exposure re-vetting —
    so the freeze is the boundary and its failures must stay
    class/path-only."""
    marker = "UNIQUEVALUEMARKER7qz"
    base = tmp_path / "case"
    home, launch = _present_pair(base)
    (home / ".megali" / "secrets.env").write_text(f"API_KEY={marker}\n")
    (home / ".megali" / "secrets.yaml").write_text(
        _policy(("API_KEY", ["api.example.com"], None))
    )

    def _attempt(label, snippet):
        return (
            f"( {snippet} ) 2>/dev/null\n"
            f'printf \'{label}|%s\\n\' "$?"\n'
        )

    body = (
        _META_GATE
        + _META_VALIDATE
        + _attempt("fn-redef", "project_secrets_lexical_path() { echo pwned; }")
        + _attempt("fn-redef-private", "_project_secrets_fail() { echo pwned; }")
        + _attempt("assign-names", "PROJECT_SECRET_NAMES=(pwned)")
        + _attempt("assign-pair", "PROJECT_SECRETS_PAIR_STATE=none")
        + _attempt("assign-dir", "PROJECT_SECRETS_DIR_LEXICAL=/tmp/pwned")
        + _attempt("assign-values", "PROJECT_SECRETS_VALUES_LEXICAL=/tmp/pwned")
        + _attempt("assign-allow", "_PROJECT_SECRETS_POLICY_ALLOW=(pwned)")
        + _attempt("assign-inject", "_PROJECT_SECRETS_POLICY_INJECT=(pwned)")
        + _attempt("assign-marker", "_PROJECT_SECRETS_EXPOSURE_STATE=pwned")
        + _attempt("fn-unset", "unset -f project_secrets_is_guest_name")
        + _attempt("names-unset", "unset PROJECT_SECRET_NAMES")
        + _attempt("assign-descriptor-count", "_PROJECT_SECRETS_DESCRIPTOR_COUNT=0")
        + _attempt("assign-descriptor-kind", "_PROJECT_SECRETS_DESCRIPTOR_KINDS[0]=pwned")
        + _attempt("assign-pinned", "_PROJECT_SECRETS_HELPERS_PINNED=0")
        + _attempt("assign-msbfd", "_PROJECT_SECRETS_MSB_FD=pwned")
        + "project_secrets_validate_metadata\n"
        + 'printf \'meta2|%s\\n\' "$?"\n'
        + 'project_secrets_discover "$1" "$2" default ""\n'
        + 'printf \'rediscover|%s\\n\' "$?"\n'
        + "project_secrets_register_projects_root_scan\n"
        + 'printf \'reregister|%s\\n\' "$?"\n'
        + "project_secrets_preflight_exposure\n"
        + 'printf \'repreflight|%s\\n\' "$?"\n'
        + "project_secrets_check_runtime\n"
        + 'printf \'recheck|%s\\n\' "$?"\n'
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'repin|%s\\n\' "$?"\n'
        + 'project_secrets_resolve_runtime "$3"\n'
        + 'printf \'reresolve|%s\\n\' "$?"\n'
        + 'printf \'names|%s\\n\' "${PROJECT_SECRET_NAMES[*]}"\n'
        + 'printf \'count|%s\\n\' "$_PROJECT_SECRETS_DESCRIPTOR_COUNT"\n'
    )
    result = run_library(body, [launch, home, _meta_runtime_path(base)])
    steps = _steps(result)
    assert steps["meta"] == "0"
    for label in (
        "fn-redef", "fn-redef-private", "assign-names", "assign-pair",
        "assign-dir", "assign-values", "assign-allow", "assign-inject",
        "assign-marker", "fn-unset", "names-unset", "assign-descriptor-count",
        "assign-descriptor-kind", "assign-pinned", "assign-msbfd",
    ):
        assert steps[label] != "0", label
    assert steps["meta2"] != "0"
    assert steps["rediscover"] != "0"
    assert steps["reregister"] != "0"
    assert steps["repreflight"] != "0"
    assert steps["recheck"] != "0"
    assert steps["repin"] != "0"
    assert steps["reresolve"] != "0"
    assert steps["names"] == "API_KEY"
    assert steps["count"] == "1"
    # Every diagnostic is the library's class/path form: each of the
    # post-freeze lifecycle calls failed with the documented class, no raw
    # shell readonly error leaked, and no secret value appears anywhere.
    for line in result.stderr.splitlines():
        assert line.startswith("project-secrets: "), line
    assert result.stderr.count("metadata-already-validated") == 7
    assert "readonly" not in result.stderr
    assert marker not in result.stdout + result.stderr


def test_frozen_descriptor_and_pin_state_survive_trusted_config(tmp_path):
    """Prove the freeze closes the descriptor-registry and pin-bookkeeping
    gap: after a successful metadata validation, trusted ordinary
    configuration that sets descriptor or pin state fails and changes
    nothing, and the frozen registry still answers the post-freeze runtime
    revalidation that value retention depends on, so an unchanged pinned
    runtime revalidates successfully.

    A mutable descriptor count would let configuration zero the registry
    so a runtime or helper hard-linked into a registered tree escapes
    exposure re-vetting before value retention, and a mutable pin marker
    or pinned descriptor would let it redirect which executable identity
    is trusted; Task 5's value-retention step keeps calling
    project_secrets_revalidate_runtime after the freeze, so that call must
    still succeed over the frozen state."""
    marker = "UNIQUEVALUEMARKER7qz"
    base = tmp_path / "case"
    home, launch = _present_pair(base)
    (home / ".megali" / "secrets.env").write_text(f"API_KEY={marker}\n")
    (home / ".megali" / "secrets.yaml").write_text(
        _policy(("API_KEY", ["api.example.com"], None))
    )

    def _attempt(label, snippet):
        return (
            f"( {snippet} ) 2>/dev/null\n"
            f'printf \'{label}|%s\\n\' "$?"\n'
        )

    body = (
        _META_GATE
        + _META_VALIDATE
        + _attempt("set-count", "_PROJECT_SECRETS_DESCRIPTOR_COUNT=0")
        + _attempt("set-kinds", '_PROJECT_SECRETS_DESCRIPTOR_KINDS=(pwned)')
        + _attempt("set-lex", '_PROJECT_SECRETS_DESCRIPTOR_LEX=(pwned)')
        + _attempt("set-phys", '_PROJECT_SECRETS_DESCRIPTOR_PHYS=(pwned)')
        + _attempt("set-pinned", "_PROJECT_SECRETS_HELPERS_PINNED=0")
        + _attempt("set-msbfd", "_PROJECT_SECRETS_MSB_FD=pwned")
        + 'printf \'count|%s\\n\' "$_PROJECT_SECRETS_DESCRIPTOR_COUNT"\n'
        + 'printf \'kind0|%s\\n\' "${_PROJECT_SECRETS_DESCRIPTOR_KINDS[0]}"\n'
        + 'printf \'phys0|%s\\n\' "${_PROJECT_SECRETS_DESCRIPTOR_PHYS[0]}"\n'
        + 'printf \'pinned|%s\\n\' "$_PROJECT_SECRETS_HELPERS_PINNED"\n'
        + 'printf \'msbfd|%s\\n\' "$_PROJECT_SECRETS_MSB_FD"\n'
        + "project_secrets_revalidate_runtime\n"
        + 'printf \'revalidate|%s\\n\' "$?"\n'
        + 'printf \'names|%s\\n\' "${PROJECT_SECRET_NAMES[*]}"\n'
    )
    result = run_library(body, [launch, home, _meta_runtime_path(base)])
    steps = _steps(result)
    assert steps["meta"] == "0"
    for label in ("set-count", "set-kinds", "set-lex", "set-phys",
                  "set-pinned", "set-msbfd"):
        assert steps[label] != "0", label
    # The frozen registry and pin bookkeeping still hold the values the
    # validated preflight and resolution established.
    assert steps["count"] == "1"
    assert steps["kind0"] == "tree-no-follow"
    assert steps["phys0"] == str((home / "Projects").resolve())
    assert steps["pinned"] == "1"
    assert steps["msbfd"] == "65"
    # Post-freeze revalidation of the unchanged pinned runtime still
    # succeeds over the frozen state, so value retention stays reachable.
    assert steps["revalidate"] == "0"
    assert steps["names"] == "API_KEY"
    assert result.stderr == ""
    assert marker not in result.stdout + result.stderr


def test_value_grammar_literal_and_line_boundaries(tmp_path):
    """Prove the literal value grammar accepts exactly the documented
    line forms — full-line comments, blank and space-only lines, CRLF, a
    final unterminated assignment, space-only values, and values carrying
    equals signs, quotes, '#', and '$' — without executing shell syntax,
    recording names in value-file order.

    Values are forwarded later as literal data, so any evaluation or line
    reshaping here would corrupt or execute user data; name order is the
    contract later synthetic-source allocation depends on."""
    pwned = tmp_path / "pwned.txt"
    names = [
        "API_KEY", "EQUALS", "QUOTED", "HASH", "DOLLAR", "SPACE",
        "TRAIL", "SYMBOLS", "CMD",
    ]
    entries = [
        "# full-line comment",
        "   # indented comment",
        "",
        "   ",
        "API_KEY=plain value with spaces",
        "EQUALS=a=b=c",
        "QUOTED=\"double 'single' mixed\"",
        "HASH=value # not a comment",
        "DOLLAR=$VAR ${OTHER} $(true)",
        "SPACE=   ",
        "TRAIL=kept trailing   ",
        "SYMBOLS=;|&*?![](){}<>~^%",
        f"CMD=$(touch '{pwned}')`touch '{pwned}'`",
    ]
    env_text = ""
    for index, entry in enumerate(entries):
        if index == len(entries) - 1:
            terminator = ""
        elif index % 2 == 0:
            terminator = "\r\n"
        else:
            terminator = "\n"
        env_text += entry + terminator
    policy = _policy(*[(name, ["api.example.com"], None) for name in names])
    body = (
        _META_VALIDATE
        + 'printf \'names|%s\\n\' "${PROJECT_SECRET_NAMES[*]}"\n'
        + "count=0\n"
        + "for name in \"${PROJECT_SECRET_NAMES[@]}\"; do\n"
        + "  project_secrets_is_guest_name \"$name\" && count=$((count + 1))\n"
        + "done\n"
        + 'printf \'guest-count|%s\\n\' "$count"\n'
        + "project_secrets_is_guest_name NOT_A_NAME\n"
        + 'printf \'guest-bad|%s\\n\' "$?"\n'
    )
    result = _meta_run(tmp_path / "case", env_text, policy, body)
    steps = _steps(result)
    assert steps["meta"] == "0"
    assert steps["names"] == " ".join(names)
    assert steps["guest-count"] == str(len(names))
    assert steps["guest-bad"] != "0"
    assert not pwned.exists()


def test_value_grammar_rejects_bytes_and_entries_safely(tmp_path):
    """Prove every rejected value byte and entry class fails with the
    source, line number, and class only: NUL, control, and non-ASCII
    bytes, tabs, invalid or leading-whitespace names, missing equals
    signs, duplicate names, and zero-length values.

    Each diagnostic must locate the error without echoing the entry, so a
    failing launch never prints secret names or values."""
    policy = _policy(("GOOD", ["api.example.com"], None))
    cases = [
        (b"TOPSECRET=va\x00lue\n", "value-invalid-byte", 2),
        (b"TOPSECRET=va\x01lue\n", "value-invalid-byte", 2),
        (b"TOPSECRET=caf\xc3\xa9\n", "value-invalid-byte", 2),
        (b"TOPSECRET=va\tlue\n", "value-invalid-byte", 2),
        (b"TOPSECRET-NAME=v\n", "value-malformed-entry", 2),
        (b"9TOPSECRET=v\n", "value-malformed-entry", 2),
        (b" TOPSECRET=v\n", "value-malformed-entry", 2),
        (b"TOPSECRETVALUE\n", "value-malformed-entry", 2),
        (b"TOPSECRET=1\nTOPSECRET=2\n", "value-duplicate-name", 3),
        (b"TOPSECRET=\n", "value-empty-value", 2),
    ]
    for index, (bad, error_class, line) in enumerate(cases):
        result = _meta_run_env_bytes(
            tmp_path / f"case{index}", b"GOOD=1\n" + bad, policy
        )
        steps = _steps(result)
        assert steps["meta"] != "0", (bad, error_class)
        assert error_class in result.stderr, (bad, error_class)
        assert f"line {line}" in result.stderr, (bad, error_class)
        assert "TOPSECRET" not in result.stdout + result.stderr


def test_reserved_name_categories_reject(tmp_path):
    """Prove reserved-name rejection covers every category — exact
    runner-owned names, Bash-managed names, and the BASH,
    TAU_ENTRYPOINT_, and TAU_SANDBOX_SECRET_SOURCE_ prefixes — in both
    the value and policy name sets, reporting class and line while never
    printing the name itself, and that matching is case-sensitive so only
    exact reserved spellings reject.

    A reserved name would let a secret overwrite launcher or entrypoint
    state or be silently dropped by the guest, so it must fail before
    sandbox creation; printing the name would itself disclose which
    reserved namespace was targeted."""
    exact = [
        "HOME", "PATH", "PS1", "_", "TAU_NO_UPDATE_CHECK",
        "TAU_SANDBOX_SHARED_CREDENTIALS", "OPTIND", "OPTARG", "DIRSTACK",
        "PIPESTATUS", "RANDOM", "EPOCHREALTIME", "SRANDOM", "BASHPID",
        "COLUMNS", "LINES", "IFS", "SHELLOPTS", "BASH_ENV", "ENV",
        "LD_PRELOAD", "NODE_OPTIONS", "FUNCNAME", "REPLY", "MAPFILE",
        "HISTCMD",
    ]
    prefixed = [
        "BASH_VERSION", "BASH_OWNED_NAME",
        "TAU_ENTRYPOINT_SCRATCH",
        "TAU_SANDBOX_SECRET_SOURCE_1",
    ]
    for index, name in enumerate(exact + prefixed):
        policy = _policy((name, ["api.example.com"], None))
        base = tmp_path / f"v{index}"
        result = _meta_run(base, f"{name}=value\n", policy, _META_VALIDATE)
        steps = _steps(result)
        assert steps["meta"] != "0", name
        assert "reserved-name" in result.stderr, name
        assert "line 1" in result.stderr, name
        # The diagnostic carries class, path, and line only; the tmp path
        # itself contains underscores, so it is removed before checking
        # the name is never echoed.
        redacted = result.stderr.replace(
            str(base / "home" / ".megali" / "secrets.env"), ""
        )
        assert name not in result.stdout + redacted
    # Policy header names are reserved identically.
    result = _meta_run(
        tmp_path / "p0",
        "GOOD=1\n",
        "HOME:\n  allow:\n    - api.example.com\n",
        _META_VALIDATE,
    )
    steps = _steps(result)
    assert steps["meta"] != "0"
    assert "reserved-name" in result.stderr
    assert "line 1" in result.stderr
    redacted = result.stderr.replace(
        str(tmp_path / "p0" / "home" / ".megali" / "secrets.yaml"), ""
    )
    assert "HOME" not in result.stdout + redacted
    # Case-sensitive control: lowercase spellings are ordinary names.
    names = ["home", "bash_thing", "tau_entrypoint_x"]
    policy = _policy(*[(name, ["api.example.com"], None) for name in names])
    env = "".join(f"{name}=1\n" for name in names)
    result = _meta_run(tmp_path / "control", env, policy, _META_VALIDATE)
    assert _steps(result)["meta"] == "0"


def test_policy_minimal_default_and_full_forms(tmp_path):
    """Prove the policy grammar accepts the minimal form with the headers
    default, and the full form — comments and blank lines between
    elements, quoted and unquoted destinations, a wildcard, all three
    injection locations, and a final unterminated item — canonicalizing
    destinations to lowercase and recording effective state per secret.

    The effective model is exactly what the generated runtime policy must
    encode later: an omitted inject field means headers only, and
    lowercase canonical destinations drive duplicates and generation."""
    env = "A=1\nB=2\nC=3\n"

    minimal_env = "A=1\n"
    minimal = _policy(("A", ["api.example.com"], None))
    result = _meta_run(tmp_path / "minimal", minimal_env, minimal, _EFFECTIVE_BODY)
    steps = _steps(result)
    assert steps["meta"] == "0"
    assert steps["names"] == "A"
    assert steps["allow0"] == "api.example.com"
    assert steps["inject0"] == "headers"

    full = (
        "# leading comment\n"
        "\n"
        "A:\n"
        "  allow:\n"
        "    - \"Api.Example.COM\"\n"
        "   # indented comment\n"
        "\n"
        "    - '*.internal.io'\n"
        "\n"
        "B:\n"
        "  # comment before field\n"
        "  allow:\n"
        "    - svc.example.com\n"
        "  inject:\n"
        "    - headers\n"
        "    - basic_auth\n"
        "    - query_params\n"
        "C:\n"
        "  allow:\n"
        "    - a.io\n"
        "    - 'b.io'\n"
        "  inject:\n"
        "    - query_params"
    )
    result = _meta_run(tmp_path / "full", env, full, _EFFECTIVE_BODY)
    steps = _steps(result)
    assert steps["meta"] == "0"
    assert steps["names"] == "A B C"
    assert steps["allow0"] == "api.example.com *.internal.io"
    assert steps["inject0"] == "headers"
    assert steps["allow1"] == "svc.example.com"
    assert steps["inject1"] == "headers basic_auth query_params"
    assert steps["allow2"] == "a.io b.io"
    assert steps["inject2"] == "query_params"


def test_policy_syntax_rejection_matrix(tmp_path):
    """Prove the policy parser rejects every unsupported construct at the
    correct line: BOM, tabs, non-ASCII bytes, trailing whitespace, bad
    indentation, wrong field order, repeated fields, inline comments,
    collections, anchors, aliases, tags, escapes, unmatched quotes, empty
    scalars, missing list items, unknown fields and injection values, and
    interpolation markers.

    Each rejection must name the offending line so the user can fix the
    policy without the launcher accepting a looser interpretation than
    the restricted grammar defines."""
    cases = [
        ("bom", b"\xef\xbb\xbfKEY:\n  allow:\n    - a.io\n", 1, "policy-invalid-byte"),
        ("tab-indent", b"KEY:\n\tallow:\n", 2, "policy-invalid-byte"),
        ("tab-item", b"KEY:\n  allow:\n    -\ta.io\n", 3, "policy-invalid-byte"),
        ("non-ascii", "KEY:\n  allow:\n    - caf\xe9.io\n".encode("latin-1"), 3, "policy-invalid-byte"),
        ("trailing-space-field", b"KEY:\n  allow: \n    - a.io\n", 2, "policy-syntax"),
        ("trailing-space-header", b"KEY: \n  allow:\n    - a.io\n", 1, "policy-syntax"),
        ("one-space-indent", b"KEY:\n allow:\n", 2, "policy-syntax"),
        ("three-space-indent", b"KEY:\n   allow:\n", 2, "policy-syntax"),
        ("five-space-item", b"KEY:\n  allow:\n     - a.io\n", 3, "policy-syntax"),
        ("two-space-item", b"KEY:\n  allow:\n  - a.io\n", 3, "policy-syntax"),
        ("item-before-field", b"KEY:\n    - a.io\n", 2, "policy-syntax"),
        ("inject-before-allow", b"KEY:\n  inject:\n    - headers\n", 2, "policy-syntax"),
        ("allow-field-twice", b"KEY:\n  allow:\n    - a.io\n  allow:\n    - b.io\n", 4, "policy-duplicate"),
        ("inject-field-twice", b"KEY:\n  allow:\n    - a.io\n  inject:\n    - headers\n  inject:\n    - headers\n", 6, "policy-duplicate"),
        ("unknown-field", b"KEY:\n  value:\n    - a.io\n", 2, "policy-syntax"),
        ("inline-value-field", b"KEY:\n  allow: [a.io]\n", 2, "policy-syntax"),
        ("inline-comment-field", b"KEY:\n  allow: # c\n", 2, "policy-syntax"),
        ("inline-comment-item", b"KEY:\n  allow:\n    - a.io # c\n", 3, "policy-destination"),
        ("collection-item", b"KEY:\n  allow:\n    - [a.io]\n", 3, "policy-destination"),
        ("anchor-item", b"KEY:\n  allow:\n    - &anchor\n", 3, "policy-destination"),
        ("alias-item", b"KEY:\n  allow:\n    - *alias\n", 3, "policy-destination"),
        ("tag-item", b"KEY:\n  allow:\n    - !!str a.io\n", 3, "policy-destination"),
        ("escape-item", b"KEY:\n  allow:\n    - a\\.io\n", 3, "policy-destination"),
        ("unmatched-single-quote", b"KEY:\n  allow:\n    - 'a.io\n", 3, "policy-destination"),
        ("unmatched-double-quote", b'KEY:\n  allow:\n    - "a.io\n', 3, "policy-destination"),
        ("empty-scalar", b"KEY:\n  allow:\n    - \n", 3, "policy-destination"),
        ("empty-quoted-scalar", b"KEY:\n  allow:\n    - ''\n", 3, "policy-destination"),
        ("bare-dash", b"KEY:\n  allow:\n    -\n", 3, "policy-syntax"),
        ("interpolation-item", b"KEY:\n  allow:\n    - ${KEY}.io\n", 3, "policy-destination"),
        ("dollar-item", b"KEY:\n  allow:\n    - $host.io\n", 3, "policy-destination"),
        ("unknown-inject-value", b"KEY:\n  allow:\n    - a.io\n  inject:\n    - cookies\n", 5, "policy-syntax"),
        ("quoted-inject-value", b'KEY:\n  allow:\n    - a.io\n  inject:\n    - "headers"\n', 5, "policy-syntax"),
        ("header-inline-value", b"KEY: value\n  allow:\n    - a.io\n", 1, "policy-syntax"),
        ("header-no-colon", b"KEY\n  allow:\n", 1, "policy-syntax"),
        ("header-invalid-name", b"KEY-NAME:\n  allow:\n", 1, "policy-syntax"),
        ("item-without-secret", b"    - a.io\n", 1, "policy-syntax"),
        ("field-without-secret", b"  allow:\n    - a.io\n", 1, "policy-syntax"),
        ("allow-no-items-next-header", b"KEY:\n  allow:\nNEXT:\n  allow:\n    - a.io\n", 3, "policy-syntax"),
        ("inject-no-items-eof", b"KEY:\n  allow:\n    - a.io\n  inject:\n", 4, "policy-syntax"),
        ("header-no-allow-eof", b"KEY:\n", 1, "policy-syntax"),
    ]
    for name, yaml_bytes, line, error_class in cases:
        result = _meta_run_yaml_bytes(tmp_path / name, "KEY=1\n", yaml_bytes)
        steps = _steps(result)
        assert steps["meta"] != "0", name
        assert error_class in result.stderr, name
        assert f"line {line}" in result.stderr, name


def test_destination_label_and_length_boundaries(tmp_path):
    """Prove destination label and length boundaries: an exact hostname
    of exactly 253 characters and a wildcard suffix of exactly 253
    characters (a 255-character complete scalar) are accepted, while
    single labels, IP literals, ports, `*`, invalid labels, and overlength
    forms reject with the offending line.

    The runtime allowlist must encode whole DNS names only; a laxer
    destination grammar would widen substitution beyond the validated
    services."""
    label62 = "a" * 62
    label63 = "b" * 63
    exact253 = f"{label62}.{label63}.{label63}.{label62}"
    assert len(exact253) == 253
    exact254 = f"{label63}.{label63}.{label63}.{label62}"
    assert len(exact254) == 254
    valid = [exact253, f"*.{exact253}", "a-b.c9.io", "9start.example.com"]
    for index, dest in enumerate(valid):
        policy = _policy(("KEY", [dest], None))
        body = _META_VALIDATE + 'printf \'allow0|%s\\n\' "${_PROJECT_SECRETS_POLICY_ALLOW[0]}"\n'
        result = _meta_run(tmp_path / f"ok{index}", "KEY=1\n", policy, body)
        steps = _steps(result)
        assert steps["meta"] == "0", dest
        assert steps["allow0"] == dest.lower(), dest
    invalid = [
        "example",
        "*",
        "*.example",
        "192.168.1.1",
        "example.com:8080",
        "-a.io",
        "a-.io",
        "a_b.io",
        "c" * 64 + ".io",
        exact254,
        f"*.{exact254}",
        "example.com.",
    ]
    for index, dest in enumerate(invalid):
        policy = _policy(("KEY", [dest], None))
        result = _meta_run(tmp_path / f"bad{index}", "KEY=1\n", policy)
        steps = _steps(result)
        assert steps["meta"] != "0", dest
        assert "policy-destination" in result.stderr, dest
        assert "line 3" in result.stderr, dest


def test_policy_duplicates_empty_lists_and_name_mismatch(tmp_path):
    """Prove duplicate policy data and name-set mismatches reject:
    repeated names, fields, canonical (case-insensitive) destinations,
    and injection values; empty list data; names present in only one
    source or differing by case; and an empty pair — with no ambient
    fallback for a name missing from the policy.

    Duplicates would double-encode substitution rules, and a name-set
    mismatch resolved from the ambient environment would inject a value
    the policy never authorized."""
    both = "KEY=1\nOTHER=2\n"
    cases = [
        ("duplicate-name", "KEY=1\n",
         "KEY:\n  allow:\n    - a.io\nKEY:\n  allow:\n    - b.io\n",
         "policy-duplicate", 4),
        ("duplicate-canonical-host", "KEY=1\n",
         "KEY:\n  allow:\n    - Example.com\n    - example.com\n",
         "policy-duplicate", 4),
        ("duplicate-inject-value", "KEY=1\n",
         "KEY:\n  allow:\n    - a.io\n  inject:\n    - headers\n    - headers\n",
         "policy-duplicate", 6),
        ("allow-no-items-eof", "KEY=1\n", "KEY:\n  allow:\n",
         "policy-syntax", 2),
        ("case-mismatch", "KEY=1\n", "Key:\n  allow:\n    - a.io\n",
         "policy-name-mismatch", None),
        ("name-only-in-values", both, _policy(("KEY", ["a.io"], None)),
         "policy-name-mismatch", None),
        ("name-only-in-policy", "KEY=1\n",
         _policy(("KEY", ["a.io"], None), ("OTHER", ["b.io"], None)),
         "policy-name-mismatch", None),
        ("empty-pair", "# comment only\n", "# comment only\n",
         "policy-empty", None),
    ]
    for index, (name, env, policy, error_class, line) in enumerate(cases):
        result = _meta_run(tmp_path / f"case{index}", env, policy)
        steps = _steps(result)
        assert steps["meta"] != "0", name
        assert error_class in result.stderr, name
        if line is not None:
            assert f"line {line}" in result.stderr, name
    # No ambient fallback: a policy missing a name fails even when the
    # launcher environment already exports that name.
    base = tmp_path / "ambient"
    home = base / "home"
    launch = home / "Projects" / "megali"
    launch.mkdir(parents=True)
    _make_pair(home / ".megali", both, _policy(("KEY", ["a.io"], None)))
    result = run_library(
        "export OTHER=ambient\n" + _META_GATE + _META_VALIDATE,
        [launch, home, _meta_runtime_path(tmp_path / "ambient")],
    )
    assert _steps(result)["meta"] != "0"


def test_metadata_never_retains_real_values(tmp_path):
    """Prove a successful metadata validation retains no real value: a
    marker present only in the value text appears in no shell variable
    after the call, the call produces no output, and the guest-name
    predicate answers correctly while emitting nothing.

    Value retention before ordinary configuration runs would let trusted
    environment files and inherited tracing observe secret material; the
    metadata pass must keep names and policy state only."""
    marker = "UNIQUEVALUEMARKER7qz"
    env = f"API_KEY={marker}\nOTHER=plain\n"
    policy = _policy(
        ("API_KEY", ["api.example.com"], None),
        ("OTHER", ["b.io"], ("basic_auth",)),
    )
    body = (
        _META_VALIDATE
        + 'printf \'names|%s\\n\' "${PROJECT_SECRET_NAMES[*]}"\n'
        + "project_secrets_is_guest_name API_KEY\n"
        + 'printf \'isguest|%s\\n\' "$?"\n'
        + "project_secrets_is_guest_name ABSENT\n"
        + 'printf \'notguest|%s\\n\' "$?"\n'
        + 'emitted="$(project_secrets_is_guest_name API_KEY 2>&1)"\n'
        + 'printf \'emitted|%s\\n\' "$emitted"\n'
        + "set\n"
    )
    result = _meta_run(tmp_path / "case", env, policy, body)
    steps = _steps(result)
    assert steps["meta"] == "0"
    assert steps["names"] == "API_KEY OTHER"
    assert steps["isguest"] == "0"
    assert steps["notguest"] != "0"
    assert steps["emitted"] == ""
    assert marker not in result.stdout + result.stderr


def test_env_source_aliases_reject_without_execution(tmp_path):
    """Prove environment-source validation rejects a TAU_ENV_FILE that is
    a lexical, physical, or hard-link alias of the present-pair value
    source before any sourcing could happen, that an absent file stays
    valid, and that command substitution inside the aliased file never
    executes.

    Sourcing the value source through an alias would export every secret
    into the ordinary environment; the identity check must run first and
    must never evaluate file content."""
    home, launch = _present_pair(tmp_path)
    secret = home / ".megali"
    pwned = tmp_path / "pwned.txt"
    payload = f"LEAK=$(touch '{pwned}')\n"
    (secret / "secrets.env").write_text(payload)
    alias_link = tmp_path / "env-link"
    alias_link.symlink_to(secret / "secrets.env")
    alias_hard = tmp_path / "env-hard"
    os.link(secret / "secrets.env", alias_hard)
    unrelated = tmp_path / "unrelated.env"
    unrelated.write_text(payload)
    body = (
        _PAIR_GATE
        + 'project_secrets_validate_env_source "$3"\n'
        + 'printf \'env|%s\\n\' "$?"\n'
    )
    for name, path in [
        ("lexical", secret / "secrets.env"),
        ("lexical-dots", home / "Projects/../.megali/./secrets.env"),
        ("symlink", alias_link),
        ("hardlink", alias_hard),
    ]:
        result = run_library(body, [launch, home, path])
        steps = _steps(result)
        assert steps["env"] != "0", name
        assert "env-source-alias" in result.stderr, name
        assert not pwned.exists(), name
    # An absent environment file remains a valid ordinary source.
    result = run_library(body, [launch, home, tmp_path / "missing.env"])
    assert _steps(result)["env"] == "0"
    # An unrelated file is valid (identity is the only criterion) and is
    # never sourced by the check itself.
    result = run_library(body, [launch, home, unrelated])
    assert _steps(result)["env"] == "0"
    assert not pwned.exists()
    # Without a present pair there is no value source to alias.
    bare = _make_home(tmp_path / "bare")
    (bare / "Projects" / "megali").mkdir(parents=True)
    result = run_library(
        body, [bare / "Projects" / "megali", bare, secret / "secrets.env"]
    )
    assert _steps(result)["env"] == "0"


# ---------------------------------------------------------------------------
# Staging lifecycle and isolated invocation.

def _runtime_shim(base, status=0, argv_log=None, env_log=None, conf_log=None,
                  version=b"msb 0.6.12\n", delay=0):
    """A fake runtime executable in its own directory; returns the PATH
    prefix holding it.

    It satisfies the exact ``--version`` contract, and for ``run``
    invocations logs every argv element, dumps its process environment,
    and copies the generated policy file it was pointed at, so tests can
    prove exactly what the isolated child handed the runtime."""
    directory = base / "msb dir"
    directory.mkdir(parents=True, exist_ok=True)
    body = "#!/bin/bash\n"
    if version:
        body += "printf '" + "".join(f"\\x{b:02x}" for b in version) + "'\n"
    body += 'if [[ "$1" == "--version" ]]; then exit 0; fi\n'
    if argv_log is not None:
        body += f'for _a in "$@"; do printf \'ARG|%s\\n\' "$_a" >> "{argv_log}"; done\n'
    if env_log is not None:
        body += f'env > "{env_log}"\n'
    if conf_log is not None:
        body += (
            "_conf=\"\" _prev=\"\"\n"
            "for _a in \"$@\"; do\n"
            "  if [[ -z \"$_conf\" && \"$_prev\" == \"--secret-conf\" ]]; then _conf=\"$_a\"; fi\n"
            "  _prev=\"$_a\"\n"
            "done\n"
            f'printf \'CONF|%s\\n\' "$_conf" >> "{argv_log}"\n'
            f'if [[ -n "$_conf" && -f "$_conf" ]]; then cp "$_conf" "{conf_log}"; fi\n'
        )
    if delay:
        body += f"sleep {delay}\n"
    body += f"exit {status}\n"
    _shim(directory, "msb", body)
    return f"{directory}:{_SYSTEM_PATH}"


def _runtime_env_sources(env_log):
    """The synthetic source variables a fake runtime saw, as a dict."""
    sources = {}
    for line in env_log.read_text().splitlines():
        if line.startswith("TAU_SANDBOX_SECRET_SOURCE_"):
            name, _, value = line.partition("=")
            sources[name] = value
    return sources


# The full gate the isolated invocation requires: discovery, the
# projects-root scan, pinned helpers, a preflighted exposure registry, an
# established compatible runtime, pre-freeze staging creation, and frozen
# validated metadata.
_EXEC_GATE = (
    _META_GATE
    + "project_secrets_create_staging\n"
    + "project_secrets_validate_metadata\n"
)


def test_create_staging_is_prefreeze_and_private(tmp_path):
    """Prove staging creation is a pre-freeze lifecycle step that produces
    a private, empty staging area: one mode-0700 directory under fixed
    host /tmp, a predefined mode-0600 empty policy path, both recorded,
    and no value text anywhere inside; a post-freeze call fails with the
    class/path-only metadata-already-validated diagnostic and creates
    nothing.

    Registration and state freeze at validate_metadata, so staging must
    be created and exposure-checked before it; the staging area exists
    while trusted ordinary configuration runs, so it may never contain
    value text, and a post-freeze creation attempt must fail cleanly
    instead of mutating frozen state."""
    marker = "UNIQUEVALUEMARKER7qz"
    base = tmp_path / "case"
    body = (
        _META_GATE
        + "project_secrets_create_staging\n"
        + 'printf \'staging|%s\\n\' "$?"\n'
        + 'printf \'sdir|%s\\n\' "${PROJECT_SECRETS_STAGING_DIR-}"\n'
        + 'printf \'sconf|%s\\n\' "${PROJECT_SECRETS_GENERATED_CONF-}"\n'
        + "project_secrets_validate_metadata\n"
        + 'printf \'meta|%s\\n\' "$?"\n'
        + "project_secrets_create_staging\n"
        + 'printf \'staging2|%s\\n\' "$?"\n'
    )
    result = _meta_run(base, f"API_KEY={marker}\n",
                       _policy(("API_KEY", ["api.example.com"], None)), body)
    steps = _steps(result)
    assert steps["staging"] == "0"
    assert steps["meta"] == "0"
    staging = pathlib.Path(steps["sdir"])
    assert str(staging).startswith("/tmp/")
    assert steps["sconf"] == str(staging / "secrets.conf")
    assert (staging.stat().st_mode & 0o777) == 0o700
    conf = staging / "secrets.conf"
    assert (conf.stat().st_mode & 0o777) == 0o600
    assert conf.read_text() == ""
    assert not any(
        path.is_file() and marker in path.read_text()
        for path in staging.rglob("*")
    )
    # Post-freeze creation fails class/path-only with the frozen policy
    # path: exactly one diagnostic line, no raw readonly error, no values.
    assert steps["staging2"] != "0"
    assert result.stderr.splitlines() == [
        f"project-secrets: metadata-already-validated: {base}/home/.megali/secrets.yaml"
    ]
    assert marker not in result.stdout + result.stderr
    shutil.rmtree(staging)


def test_staging_exposure_collision_rejects(tmp_path):
    """Prove staging creation fails staging-exposure before any value use
    when the fixed /tmp staging root lies inside a registered exposure
    domain — by lexical containment and through a physical alias — with
    no staging directory created and no value text touched.

    A staging directory inside an exposed tree would let guest-visible or
    build-visible content read the generated runtime configuration, so
    the overlap check must fire before mktemp could place anything in the
    exposed domain and before any helper or source byte is touched."""
    base = tmp_path / "case"
    home, launch = _present_pair(base)
    marker = "UNIQUEVALUEMARKER7qz"
    (home / ".megali" / "secrets.env").write_text(f"API_KEY={marker}\n")
    (home / ".megali" / "secrets.yaml").write_text(
        _policy(("API_KEY", ["api.example.com"], None))
    )
    body = (
        'project_secrets_discover "$1" "$2" default ""\n'
        "project_secrets_register_projects_root_scan\n"
        + _register_only("$3", "/tmp")
        + "project_secrets_create_staging\n"
        + 'printf \'staging|%s\\n\' "$?"\n'
        + 'printf \'sdir|%s\\n\' "${PROJECT_SECRETS_STAGING_DIR-}"\n'
    )
    for kind in ("tree-no-follow", "tree-dereference"):
        result = run_library(body, [launch, home, kind])
        steps = _steps(result)
        assert steps["staging"] != "0", kind
        assert steps["sdir"] == "", kind
        assert "staging-exposure" in result.stderr, kind
        assert marker not in result.stdout + result.stderr, kind


def test_drop_staging_reports_failed_removal(tmp_path):
    """Prove a failed best-effort staging removal is reported with a
    staging-cleanup class/path diagnostic naming the leaked staging
    directory instead of disappearing silently.

    _project_secrets_drop_staging runs on the create_staging failure
    paths; if the pinned removal helper itself fails there, the staging
    directory — with its predefined policy path — would otherwise stay
    on disk with no diagnostic telling the user where it leaked."""
    home, launch = _present_pair(tmp_path)
    shim_dir = tmp_path / "shims"
    for tool in ("tr", "cmp", "mktemp"):
        _shim(
            shim_dir, tool,
            f'#!/bin/bash\nexec "{shutil.which(tool)}" "$@"\n',
        )
    _shim(shim_dir, "chmod", "#!/bin/bash\nexit 1\n")
    _shim(shim_dir, "rm", "#!/bin/bash\nexit 1\n")
    body = (
        'project_secrets_discover "$1" "$2" default ""\n'
        + 'project_secrets_pin_helpers "$3"\n'
        + 'printf \'pin|%s\\n\' "$?"\n'
        + "project_secrets_create_staging\n"
        + 'printf \'staging|%s\\n\' "$?"\n'
    )
    result = run_library(body, [launch, home, f"{shim_dir}:{_SYSTEM_PATH}"])
    steps = _steps(result)
    assert steps["pin"] == "0"
    assert steps["staging"] != "0"
    assert "helper-failed" in result.stderr
    assert "staging-cleanup" in result.stderr
    match = re.search(
        r"project-secrets: staging-cleanup: (/tmp/project-secrets\.\S+)",
        result.stderr,
    )
    assert match, result.stderr
    leaked = pathlib.Path(match.group(1))
    try:
        assert leaked.is_dir()
    finally:
        shutil.rmtree(leaked, ignore_errors=True)



def _exec_base(tmp_path, env_text, policy, status=0, delay=0):
    """A present pair plus an instrumented fake runtime; returns
    (home, launch, PATH, logs) where logs maps argv/env/conf log paths.

    The runtime logs each argv element, dumps its process environment,
    and copies the generated policy it was pointed at, so tests can
    prove exactly what crossed the exec boundary and nothing else."""
    base = tmp_path / "case"
    home, launch = _present_pair(base)
    (home / ".megali" / "secrets.env").write_text(env_text)
    (home / ".megali" / "secrets.yaml").write_text(policy)
    logs = {
        "argv": base / "runtime-argv.log",
        "env": base / "runtime-env.log",
        "conf": base / "runtime-conf.log",
    }
    path = _runtime_shim(
        base, status=status, delay=delay, argv_log=logs["argv"],
        env_log=logs["env"], conf_log=logs["conf"],
    )
    return home, launch, path, logs


def _runtime_argv(logs):
    """The exact argv the fake runtime received, as a list."""
    return [
        line[len("ARG|"):]
        for line in logs["argv"].read_text().splitlines()
        if line.startswith("ARG|")
    ]


def _runtime_conf_dir(logs):
    """The staging directory the runtime was invoked with, from its log.

    The shim records the first ``--secret-conf`` value it saw — the one
    the library inserted immediately after ``run`` — so guest arguments
    spelled ``--secret-conf`` never mask the authoritative path."""
    for line in logs["argv"].read_text().splitlines():
        if line.startswith("CONF|"):
            return pathlib.Path(line[len("CONF|"):]).parent
    raise AssertionError("runtime did not report a secret-conf path")


def _run_exec(body, launch, home, path, timeout=60):
    """Run one full-gate isolated-invocation scenario."""
    return run_library(body, [launch, home, path], timeout=timeout)


def test_ordinary_name_collision_set(tmp_path):
    """Prove project_secrets_note_ordinary_guest_name records exactly the
    validated ordinary forwarded names in a private collision set —
    duplicates collapse, invalid names reject with a class diagnostic —
    and that source allocation skips a recorded synthetic-shaped name.

    A synthetic source colliding with an ordinary forwarded name would
    hand the guest the real value through its own -e request; the
    collision set is what keeps synthetic references collision-free."""
    marker = "UNIQUEVALUEMARKER7qz"
    home, launch, path, logs = _exec_base(
        tmp_path, f"API_KEY={marker}\n",
        _policy(("API_KEY", ["api.example.com"], None)),
    )
    body = (
        'project_secrets_note_ordinary_guest_name FOO\n'
        'printf \'note1|%s\\n\' "$?"\n'
        'project_secrets_note_ordinary_guest_name FOO\n'
        'printf \'note2|%s\\n\' "$?"\n'
        'printf \'count|%s\\n\' "${#_PROJECT_SECRETS_ORDINARY_NAMES[@]}"\n'
        'project_secrets_note_ordinary_guest_name 1BAD\n'
        'printf \'bad|%s\\n\' "$?"\n'
        'project_secrets_note_ordinary_guest_name TAU_SANDBOX_SECRET_SOURCE_0\n'
        'printf \'synthetic|%s\\n\' "$?"\n'
        + _EXEC_GATE
        + 'project_secrets_exec_runtime run -- /bin/true\n'
        + 'printf \'exec|%s\\n\' "$?"\n'
    )
    result = _run_exec(body, launch, home, path)
    steps = _steps(result)
    assert steps["note1"] == "0"
    assert steps["note2"] == "0"
    assert steps["count"] == "1"
    assert steps["bad"] != "0"
    assert "invalid-ordinary-name" in result.stderr
    assert steps["synthetic"] == "0"
    assert steps["exec"] == "0"
    conf = logs["conf"].read_text()
    assert 'value: "${TAU_SANDBOX_SECRET_SOURCE_1}"' in conf
    assert "TAU_SANDBOX_SECRET_SOURCE_0" not in conf
    assert not _runtime_conf_dir(logs).exists()


def test_source_allocation_skips_all_collision_classes(tmp_path):
    """Prove synthetic source allocation skips every collision class:
    names present in the process environment, ordinary forwarded names,
    names already allocated to an earlier secret, and occupied later
    candidates, always choosing the first free candidate.

    A candidate colliding with any of these would either overwrite host
    data, leak through a guest-forwarded name, or point two secrets at
    one source. Project guest names cannot collide at all because the
    reserved TAU_SANDBOX_SECRET_SOURCE_ prefix rejects them, so that
    class is enforced structurally by the metadata grammar."""
    env = "FIRST=one\nSECOND=two\n"
    policy = _policy(("FIRST", ["a.io"], None), ("SECOND", ["b.io"], None))
    home, launch, path, logs = _exec_base(tmp_path, env, policy)
    body = (
        'export TAU_SANDBOX_SECRET_SOURCE_0=host-zero\n'
        'project_secrets_note_ordinary_guest_name TAU_SANDBOX_SECRET_SOURCE_1\n'
        'export TAU_SANDBOX_SECRET_SOURCE_2=host-two\n'
        + _EXEC_GATE
        + 'project_secrets_exec_runtime run -- /bin/true\n'
        + 'printf \'exec|%s\\n\' "$?"\n'
    )
    result = _run_exec(body, launch, home, path)
    steps = _steps(result)
    assert steps["exec"] == "0"
    conf = logs["conf"].read_text()
    # 0, 1, 2 are occupied; FIRST takes 3; SECOND skips 0-2 and the
    # prior allocation 3, then steps over nothing else and takes 4.
    assert 'value: "${TAU_SANDBOX_SECRET_SOURCE_3}"' in conf
    assert 'value: "${TAU_SANDBOX_SECRET_SOURCE_4}"' in conf
    sources = _runtime_env_sources(logs["env"])
    assert sources["TAU_SANDBOX_SECRET_SOURCE_3"] == "one"
    assert sources["TAU_SANDBOX_SECRET_SOURCE_4"] == "two"
    # The host-owned occupied names keep their original values.
    assert sources["TAU_SANDBOX_SECRET_SOURCE_0"] == "host-zero"
    assert sources["TAU_SANDBOX_SECRET_SOURCE_2"] == "host-two"
    assert "TAU_SANDBOX_SECRET_SOURCE_1=" not in logs["env"].read_text()


def test_generated_policy_exact_effective_model(tmp_path):
    """Prove the generated policy is exactly the effective model: one
    double-quoted guest-name key per secret in value-file order
    (implicit-scalar-shaped names like true included), the three fields
    value/allow/inject in order, value as the double-quoted synthetic
    source reference, allow as double-quoted lowercase destinations,
    inject as unquoted identifiers with the headers default, and no
    additional fields or plaintext values.

    The compatible runtime parses this document with a strict YAML
    reader, so any deviation in quoting, ordering, casing, or extra
    fields would silently change which requests a placeholder may
    reach; the exact text is the compatibility contract."""
    marker_a = "UNIQUEVALUEMARKER7qz"
    marker_b = "second-marker-value"
    env = f"A_KEY={marker_a}\ntrue={marker_b}\n"
    # Policy-file order is reversed to prove value-file order wins.
    policy = _policy(
        ("true", ["b.io"], ("basic_auth", "query_params")),
        ("A_KEY", ["api.Example.COM", '"*.githubusercontent.com"'], None),
    )
    home, launch, path, logs = _exec_base(tmp_path, env, policy)
    body = _EXEC_GATE + 'project_secrets_exec_runtime run -- /bin/true\n'
    result = _run_exec(body, launch, home, path)
    assert result.returncode == 0
    conf = logs["conf"].read_text()
    assert conf == (
        '"A_KEY":\n'
        '  value: "${TAU_SANDBOX_SECRET_SOURCE_0}"\n'
        "  allow:\n"
        '    - "api.example.com"\n'
        '    - "*.githubusercontent.com"\n'
        "  inject:\n"
        "    - headers\n"
        '"true":\n'
        '  value: "${TAU_SANDBOX_SECRET_SOURCE_1}"\n'
        "  allow:\n"
        '    - "b.io"\n'
        "  inject:\n"
        "    - basic_auth\n"
        "    - query_params\n"
    )
    assert "require_tls_identity" not in conf
    assert marker_a not in conf and marker_b not in conf
    assert not _runtime_conf_dir(logs).exists()


def test_policy_generation_ignores_ambient_ifs(tmp_path):
    """Prove the generated policy is immune to an ambient IFS mutated by
    trusted ordinary configuration: with IFS set to '.' and to an
    unusual multi-character IFS before the isolated invocation, the
    generated policy still contains exactly the validated destinations
    and injection locations — whole entries, no split fragments, and no
    bare '*' fragment — while the runtime argv, exported sources,
    status, and staging cleanup stay correct.

    The policy-generation loops expand the frozen space-separated policy
    strings unquoted, so under a mutated IFS a validated
    '*.githubusercontent.com' would split into fragments — including a
    bare '*' — silently widening or corrupting the allowlist the runtime
    enforces."""
    marker = "UNIQUEVALUEMARKER7qz"
    env = f"API_KEY={marker}\n"
    policy = _policy(
        ("API_KEY", ["api.Example.COM", '"*.githubusercontent.com"'],
         ("basic_auth", "query_params")),
    )
    expected = (
        '"API_KEY":\n'
        '  value: "${TAU_SANDBOX_SECRET_SOURCE_0}"\n'
        "  allow:\n"
        '    - "api.example.com"\n'
        '    - "*.githubusercontent.com"\n'
        "  inject:\n"
        "    - basic_auth\n"
        "    - query_params\n"
    )
    for name, ifs in (("dot", "."), ("unusual", ".:_")):
        home, launch, path, logs = _exec_base(tmp_path / name, env, policy)
        body = (
            _EXEC_GATE
            + f"IFS='{ifs}'\n"
            + 'project_secrets_exec_runtime run -- /bin/true\n'
            + 'printf \'exec|%s\\n\' "$?"\n'
        )
        result = _run_exec(body, launch, home, path)
        assert result.returncode == 0, ifs
        steps = _steps(result)
        assert steps["exec"] == "0", ifs
        conf = logs["conf"].read_text()
        assert conf == expected, ifs
        assert '    - "*"\n' not in conf
        argv = _runtime_argv(logs)
        assert argv[:3] == [
            "run", "--secret-conf",
            str(_runtime_conf_dir(logs) / "secrets.conf"),
        ]
        assert _runtime_env_sources(logs["env"]) == {
            "TAU_SANDBOX_SECRET_SOURCE_0": marker
        }
        assert not _runtime_conf_dir(logs).exists()


def test_sources_exist_only_in_final_exec_subprocess(tmp_path):
    """Prove real values exist only in the final exec'd runtime process:
    the parent never gains the synthetic source or the original guest
    name before or after the call, the runtime sees the synthetic
    variable with the real value, and the guest -e argument set contains
    no synthetic name or value.

    Value presence anywhere except the final subprocess — parent
    environment, forwarded arguments — would expose the secret to
    ordinary configuration, tracing, or the guest itself."""
    marker = "UNIQUEVALUEMARKER7qz"
    home, launch, path, logs = _exec_base(
        tmp_path, f"API_KEY={marker}\n",
        _policy(("API_KEY", ["api.example.com"], None)),
    )
    probe = (
        'printf \'probe|%s:%s\\n\' '
        '"${TAU_SANDBOX_SECRET_SOURCE_0-UNSET}" "${API_KEY-UNSET}"\n'
    )
    body = (
        probe
        + _EXEC_GATE
        + probe
        + 'project_secrets_exec_runtime run -e ORDINARY=plain -- /bin/true\n'
        + 'printf \'exec|%s\\n\' "$?"\n'
        + probe
    )
    result = _run_exec(body, launch, home, path)
    steps = _steps(result)
    probes = [line.split("|", 1)[1] for line in result.stdout.splitlines()
              if line.startswith("probe|")]
    assert probes == ["UNSET:UNSET"] * 3
    assert steps["exec"] == "0"
    sources = _runtime_env_sources(logs["env"])
    assert sources == {"TAU_SANDBOX_SECRET_SOURCE_0": marker}
    argv = _runtime_argv(logs)
    assert argv[:3] == ["run", "--secret-conf", str(_runtime_conf_dir(logs) / "secrets.conf")]
    assert "-e" in argv and "ORDINARY=plain" in argv
    assert not any(
        arg.startswith("TAU_SANDBOX_SECRET_SOURCE_") or marker in arg
        for arg in argv
    )
    assert marker not in result.stdout + result.stderr


def test_literal_source_reference_text_is_not_launcher_expanded(tmp_path):
    """Prove ${OTHER} text inside a value stays literal: the runtime's
    process environment receives the printable reference text exactly,
    and the launcher never looks OTHER up — it is absent from the
    runtime environment and the parent.

    Shell-style expansion of value text would let a crafted value
    splice other variables or command output into the secret the
    runtime receives; values are data, never evaluated syntax."""
    home, launch, path, logs = _exec_base(
        tmp_path, "API_KEY=A=${OTHER}B\n",
        _policy(("API_KEY", ["api.example.com"], None)),
    )
    body = (
        _EXEC_GATE
        + 'printf \'parent|%s\\n\' "${OTHER-UNSET}"\n'
        + 'project_secrets_exec_runtime run -- /bin/true\n'
    )
    result = _run_exec(body, launch, home, path)
    assert result.returncode == 0
    assert _steps(result)["parent"] == "UNSET"
    env_text = logs["env"].read_text()
    assert "TAU_SANDBOX_SECRET_SOURCE_0=A=${OTHER}B\n" in env_text
    assert "\nOTHER=" not in env_text
    conf = logs["conf"].read_text()
    assert 'value: "${TAU_SANDBOX_SECRET_SOURCE_0}"' in conf
    assert "OTHER" not in conf


def test_library_inserts_single_secret_conf_at_exact_position(tmp_path):
    """Prove the library owns exactly one --secret-conf inserted
    immediately after run: the final runtime argv is
    run --secret-conf <generated-path> <original runtime options> --
    <original guest argv> byte-for-byte, guest arguments spelled either
    --secret-conf way survive, and caller-supplied duplicates in the
    runtime-option section plus non-run operations reject before any
    value work.

    A caller-controlled --secret-conf could redirect the runtime to an
    attacker-chosen policy, and dropping or rewriting guest arguments
    would change the guest command; the insertion position and the
    pre-value rejection are both the security contract."""
    marker = "UNIQUEVALUEMARKER7qz"
    home, launch, path, logs = _exec_base(
        tmp_path, f"API_KEY={marker}\n",
        _policy(("API_KEY", ["api.example.com"], None)),
    )
    body = (
        _EXEC_GATE
        + 'project_secrets_exec_runtime run --memory 512 -e X=1 '
        '-- /bin/echo hi --secret-conf guest --secret-conf=x\n'
    )
    result = _run_exec(body, launch, home, path)
    assert result.returncode == 0
    argv = _runtime_argv(logs)
    # The authoritative policy-path spelling is the one the runtime
    # reported being pointed at (its CONF| log), not argv[2] itself —
    # deriving the expectation from argv would be self-referential — and
    # it must still be an absolute staging path.
    conf_path = _runtime_conf_dir(logs) / "secrets.conf"
    assert conf_path.is_absolute()
    assert argv == [
        "run", "--secret-conf", str(conf_path),
        "--memory", "512", "-e", "X=1", "--",
        "/bin/echo", "hi", "--secret-conf", "guest", "--secret-conf=x",
    ]

    # Duplicates in the runtime-option section and non-run operations
    # reject before values: the runtime never runs and no value text
    # appears anywhere.
    for argv_line, error_class in (
        ("run --secret-conf /tmp/x -- cmd", "runtime-secret-conf"),
        ("run --secret-conf=/tmp/x -- cmd", "runtime-secret-conf"),
        ("version -- cmd", "runtime-operation"),
        ("run cmd", "runtime-arguments"),
    ):
        base = tmp_path / f"reject-{error_class}-{len(argv_line)}"
        home_r, launch_r, path_r, logs_r = _exec_base(
            tmp_path / f"reject-{abs(hash(argv_line))}", f"API_KEY={marker}\n",
            _policy(("API_KEY", ["api.example.com"], None)),
        )
        body = (
            _EXEC_GATE
            + f"project_secrets_exec_runtime {argv_line}\n"
            + 'printf \'exec|%s\\n\' "$?"\n'
        )
        result = _run_exec(body, launch_r, home_r, path_r)
        steps = _steps(result)
        assert steps["exec"] != "0", argv_line
        assert error_class in result.stderr, argv_line
        assert not logs_r["argv"].exists(), argv_line
        assert marker not in result.stdout + result.stderr, argv_line


def test_no_pair_exec_fails_closed(tmp_path):
    """Prove exec_runtime fails closed without a pair or without staged
    policy: every case reports one class/path diagnostic, creates no
    staging, and never runs the runtime.

    The isolated invocation is only meaningful for a validated present
    pair with generated policy; calling it in any other lifecycle state
    must fail cleanly rather than invent an empty or ambient policy."""
    home, launch, path, logs = _exec_base(
        tmp_path, "API_KEY=UNIQUEVALUEMARKER7qz\n",
        _policy(("API_KEY", ["api.example.com"], None)),
    )
    # No pair at all.
    bare = _make_home(tmp_path / "bare")
    (bare / "Projects" / "elsewhere").mkdir(parents=True)
    body = (
        'project_secrets_discover "$1" "$2" default ""\n'
        + 'project_secrets_create_staging\n'
        + 'printf \'staging|%s\\n\' "$?"\n'
        + 'project_secrets_exec_runtime run -- /bin/true\n'
        + 'printf \'exec|%s\\n\' "$?"\n'
    )
    result = run_library(body, [bare / "Projects" / "elsewhere", bare])
    steps = _steps(result)
    assert steps["staging"] != "0"
    assert steps["exec"] != "0"
    assert result.stderr.splitlines() == [
        "project-secrets: staging-unavailable: ",
        "project-secrets: staging-unavailable: ",
    ]

    # Pair validated but staging never created.
    body = (
        _META_GATE
        + "project_secrets_validate_metadata\n"
        + 'printf \'meta|%s\\n\' "$?"\n'
        + 'project_secrets_exec_runtime run -- /bin/true\n'
        + 'printf \'exec|%s\\n\' "$?"\n'
    )
    result = _run_exec(body, launch, home, path)
    steps = _steps(result)
    assert steps["meta"] == "0"
    assert steps["exec"] != "0"
    assert "staging-unavailable" in result.stderr
    assert not logs["argv"].exists()


def test_cleanup_success_failure_signal_and_idempotence(tmp_path):
    """Prove staging is removed on success, failure, signal/parent exit,
    and repeated cleanup, that only the owned staging directory is
    removed, and that the exact runtime status is preserved.

    Staging outliving the launch would leave the generated policy on
    disk; removing anything but the owned directory would destroy
    unrelated host state; and cleanup must never mask the runtime's
    exit status, which the launcher forwards as its own."""
    marker = "UNIQUEVALUEMARKER7qz"
    env = f"API_KEY={marker}\n"
    policy = _policy(("API_KEY", ["api.example.com"], None))
    innocent = pathlib.Path(
        tempfile.mkdtemp(prefix="project-secrets.innocent.")
    )

    try:
        # Success: status preserved, staging removed, cleanup idempotent.
        home, launch, path, logs = _exec_base(tmp_path / "ok", env, policy)
        body = (
            _EXEC_GATE
            + 'project_secrets_exec_runtime run -- /bin/true\n'
            + 'printf \'exec|%s\\n\' "$?"\n'
            + 'project_secrets_cleanup || exit 51\n'
            + 'project_secrets_cleanup || exit 52\n'
        )
        result = _run_exec(body, launch, home, path)
        assert _steps(result)["exec"] == "0"
        assert result.returncode == 0
        assert not _runtime_conf_dir(logs).exists()

        # Failure: the exact runtime status survives cleanup.
        home, launch, path, logs = _exec_base(
            tmp_path / "fail", env, policy, status=42,
        )
        body = _EXEC_GATE + 'project_secrets_exec_runtime run -- /bin/true\n'
        result = _run_exec(body, launch, home, path)
        assert result.returncode == 42
        assert not _runtime_conf_dir(logs).exists()

        # Signal: killing the parent mid-run triggers the EXIT safety
        # path and still removes only the owned staging directory.
        home, launch, path, logs = _exec_base(
            tmp_path / "signal", env, policy, delay=3,
        )
        body = _EXEC_GATE + 'project_secrets_exec_runtime run -- /bin/true\n'
        proc = subprocess.Popen(
            ["bash", "-c", f"source \"{LIBRARY}\"\n{body}", "library",
             str(launch), str(home), path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 10
        while not logs["argv"].exists() and time.time() < deadline:
            time.sleep(0.05)
        assert logs["argv"].exists(), "runtime never started"
        proc.terminate()
        proc.communicate(timeout=30)
        assert proc.returncode != 0
        assert not _runtime_conf_dir(logs).exists()

        # Only the owned staging directory was ever removed.
        assert innocent.exists()
    finally:
        shutil.rmtree(innocent, ignore_errors=True)


def test_exec_runtime_composes_prior_exit_trap(tmp_path):
    """Prove project_secrets_exec_runtime composes with a pre-existing
    parent EXIT trap instead of replacing it: a launcher trap installed
    before the call runs exactly once, at process exit and only after the
    library's staging cleanup — never when the call returns — and a
    second isolated invocation re-arms the exit path without chaining
    the library handler onto itself.

    The launcher installs its own EXIT cleanup before the isolated
    invocation; a library trap that silently replaced it would drop the
    launcher's teardown, and a handler chained onto itself would recurse
    or double-run the launcher cleanup at exit."""
    marker = "UNIQUEVALUEMARKER7qz"
    home, launch, path, logs = _exec_base(
        tmp_path, f"API_KEY={marker}\n",
        _policy(("API_KEY", ["api.example.com"], None)),
    )
    prior_marker = tmp_path / "case" / "prior-exit.marker"
    body = (
        _EXEC_GATE
        + f"trap 'printf prior-exit-ran > \"{prior_marker}\"' EXIT\n"
        + 'project_secrets_exec_runtime run -- /bin/true\n'
        + 'printf \'exec1|%s\\n\' "$?"\n'
        + f'if [[ -e "{prior_marker}" ]]; then printf \'prior-early|yes\\n\'; '
        + 'else printf \'prior-early|no\\n\'; fi\n'
        + 'project_secrets_exec_runtime run -- /bin/true\n'
        + 'printf \'exec2|%s\\n\' "$?"\n'
    )
    result = _run_exec(body, launch, home, path)
    steps = _steps(result)
    assert steps["exec1"] == "0"
    # The second invocation cannot succeed (the first removed staging),
    # but it still re-arms the exit path and must not chain onto itself.
    assert steps["exec2"] != "0"
    assert "staging-identity" in result.stderr
    assert steps["prior-early"] == "no"
    assert result.returncode == 0
    # At process exit both cleanups ran: the library's staging removal
    # and the launcher's prior trap, exactly once each.
    assert prior_marker.read_text() == "prior-exit-ran"
    assert not _runtime_conf_dir(logs).exists()
    assert _runtime_env_sources(logs["env"]) == {
        "TAU_SANDBOX_SECRET_SOURCE_0": marker
    }


def test_xtrace_traps_and_function_shadows_cannot_observe_values(tmp_path):
    """Prove inherited instrumentation cannot observe the final value
    pass: trusted configuration that enables xtrace and functrace/
    errtrace, installs DEBUG/RETURN/ERR/EXIT traps referencing the
    private value array, and shadows parsing builtins with observer
    functions still yields a run in which no value appears in any
    launcher output, while the readonly state replacement attempts fail
    before values are retained.

    Traps, tracing, and function shadows execute with full shell
    access; the child must clear all three before parsing a single
    value byte or the value text would reach the instrumentation."""
    marker = "UNIQUEVALUEMARKER7qz"
    home, launch, path, logs = _exec_base(
        tmp_path, f"API_KEY={marker}\n",
        _policy(("API_KEY", ["api.example.com"], None)),
    )
    probe = "'${_PROJECT_SECRETS_SOURCE_VALUES[0]-EMPTY}'"
    config = (
        "set -x\n"
        "set -E\n"
        "set -T\n"
        f"trap 'echo \"DBG:{probe}\" >&2\' DEBUG\n"
        f"trap 'echo \"RET:{probe}\" >&2\' RETURN\n"
        f"trap 'echo \"ERR:{probe}\" >&2\' ERR\n"
        f"trap 'echo \"EXT:{probe}\" >&2\' EXIT\n"
        'printf() { /bin/echo "OBS-PRINTF:$*" >&2; }\n'
        'read() { /bin/echo "OBS-READ:$*" >&2; }\n'
        'local() { /bin/echo "OBS-LOCAL:$*" >&2; }\n'
        '( PROJECT_SECRET_NAMES=(pwned) ) 2>/dev/null && exit 21\n'
        '( _project_secrets_fail() { :; } ) 2>/dev/null && exit 22\n'
        '( PROJECT_SECRETS_GENERATED_CONF=/tmp/pwned ) 2>/dev/null && exit 23\n'
    )
    body = _EXEC_GATE + config + 'project_secrets_exec_runtime run -- /bin/true\n'
    result = _run_exec(body, launch, home, path)
    assert result.returncode == 0
    assert marker not in result.stdout + result.stderr
    argv = _runtime_argv(logs)
    assert argv[:2] == ["run", "--secret-conf"]
    sources = _runtime_env_sources(logs["env"])
    assert sources == {"TAU_SANDBOX_SECRET_SOURCE_0": marker}
    assert not _runtime_conf_dir(logs).exists()


def test_command_and_builtin_shadows_cannot_block_or_observe(tmp_path):
    """Prove observer functions for every builtin and external command
    the final parsing, generation, invocation, and cleanup use can
    neither block nor observe: the exact pinned runtime still runs with
    the exact argv and generated policy, cleanup still removes staging,
    no observer receives value text, and the POSIX special builtins the
    isolation depends on cannot even be shadowed.

    Any interceptable builtin on the value path would let a function
    shadow read the secret or silently swallow it; the child must run
    only through unshadowable special builtins, removed shadows, and
    absolute pinned paths."""
    marker = "UNIQUEVALUEMARKER7qz"
    base = tmp_path / "case"
    home, launch, path, logs = _exec_base(
        tmp_path, f"API_KEY={marker}\n",
        _policy(("API_KEY", ["api.example.com"], None)), status=7,
    )
    observer_log = base / "observers.log"
    names = (
        "printf", "read", "local", "declare", "builtin", "command", "echo",
        "cd", "pwd", "shopt", "wait", "type", "enable", "test", "true",
        "false", "rm", "mktemp", "chmod", "tr", "cmp", "msb", "env", "cp",
    )
    config = "".join(
        f'{name}() {{ /bin/echo "OBS:{name}:$*" >> "{observer_log}"; }}\n'
        for name in names
    )
    special = ("export", "unset", "set", "trap", "exec", "eval",
               "readonly", "shift", "return", "exit")
    config += "".join(
        f'( {name}() {{ :; }} ) 2>/dev/null && exit {31 + index}\n'
        for index, name in enumerate(special)
    )
    config += 'printf observer-probe\n'
    body = _EXEC_GATE + config + 'project_secrets_exec_runtime run -- /bin/true\n'
    result = _run_exec(body, launch, home, path)
    assert result.returncode == 7
    argv = _runtime_argv(logs)
    assert argv[:2] == ["run", "--secret-conf"]
    conf = logs["conf"].read_text()
    assert 'value: "${TAU_SANDBOX_SECRET_SOURCE_0}"' in conf
    assert marker not in conf
    sources = _runtime_env_sources(logs["env"])
    assert sources == {"TAU_SANDBOX_SECRET_SOURCE_0": marker}
    assert not _runtime_conf_dir(logs).exists()
    observed = observer_log.read_text()
    assert "OBS:printf:observer-probe" in observed
    assert marker not in observed
    assert "TAU_SANDBOX_SECRET_SOURCE_0" not in observed


# ---------------------------------------------------------------------------
# Static source contract.

# Ambient or GNU-only tools that sensitive traversal, path, and identity
# logic must never use: only Bash builtins are allowed there.
FORBIDDEN_TOOLS = ("find", "realpath", "stat", "sort", "xargs", "sha256sum")

# Bash syntax at or beyond the 4.0-only set that the library forbids to
# stay loadable by the oldest supported Bash: associative arrays, namerefs,
# global declares, parameter transformation, case modification, negative
# array indices, mapfile/readarray, and the combined redirections.
FORBIDDEN_SYNTAX = {
    "associative arrays": r"(?:declare|local|typeset)\s+-A\b",
    "namerefs": r"(?:declare|local|typeset)\s+-n\b",
    "global declare": r"(?:declare|typeset)\s+-g\b",
    "parameter transformation": r"\$\{[^}]*@[A-Za-z]",
    "case modification": r"\$\{[^}]*[,^~]\}",
    "negative array index": r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-",
    "mapfile/readarray": r"\b(?:mapfile|readarray)\b",
    "&>> redirection": r"&>>",
    "&> redirection": r"&>",
    "|& redirection": r"\|&",
}


def _executable_lines(source):
    """Return the library's executable lines: whole-line comments are
    dropped and a trailing comment introduced by whitespace-# is removed.

    The trailing-comment heuristic is honest for this file because no
    quoted string in the library contains '#', so the scan never mistakes
    string data for a comment."""
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cut = line.find(" #")
        lines.append(line[:cut] if cut != -1 else line)
    return lines


def _function_body_span(code, name):
    """Return the (start, end) index span of one function body in the
    executable lines, from its ``name() {`` header to its closing brace."""
    start = next(
        index for index, line in enumerate(code)
        if line.startswith(name + "()")
    )
    end = next(
        index for index, line in enumerate(code[start:], start)
        if line == "}"
    )
    return start, end


def _static_violations(source):
    """Run every per-line static source-contract rule on the library
    source and return the (rule, line) violations found.

    Rules are reported by name so a caller can prove a specific rule
    catches a specific mutation; the pin-table uniqueness and gate-order
    structure checks live in the main static test."""
    code = _executable_lines(source)
    violations = []

    for tool in FORBIDDEN_TOOLS:
        for line in code:
            if re.search(rf"\b{tool}\b", line):
                violations.append((f"ambient tool: {tool}", line))

    # The five helper names may appear only in the pin table; every other
    # use must go through the readonly helper fields and the invocation
    # gate, which revalidates identity immediately before executing.
    helper_word = re.compile(r"\b(?:tr|cmp|mktemp|chmod|rm)\b")
    table_index = [
        index for index, line in enumerate(code)
        if "_PROJECT_SECRETS_HELPER_NAMES=" in line
    ]
    assert len(table_index) == 1
    for index, line in enumerate(code):
        if index != table_index[0] and helper_word.search(line):
            violations.append(("helper tool word outside the pin table", line))

    # No pinned helper field is dereferenced outside the trust gates, and
    # the pinned runtime path is dereferenced only inside the runtime
    # functions, so every helper execution flows through the
    # identity-revalidating invocation gate and the runtime executable
    # runs only from the runtime functions.
    helper_gate_spans = [
        _function_body_span(code, name)
        for name in (
            "_project_secrets_require_helper",
            "_project_secrets_invoke_helper",
            "project_secrets_pin_helpers",
            "project_secrets_resolve_runtime",
        )
    ]
    runtime_gate_spans = [
        _function_body_span(code, name)
        for name in (
            "project_secrets_resolve_runtime",
            "project_secrets_revalidate_runtime",
            "project_secrets_check_runtime",
            "project_secrets_exec_runtime",
        )
    ]

    def _inside(index, spans):
        return any(start <= index <= end for start, end in spans)

    for index, line in enumerate(code):
        if re.search(r"\$\{?PROJECT_SECRETS_HELPER_[A-Z]+", line) \
                and not _inside(index, helper_gate_spans):
            violations.append(
                ("pinned helper field dereferenced outside the gates", line)
            )
        if re.search(r"\$\{?PROJECT_SECRETS_MSB_PATH", line) \
                and not _inside(index, runtime_gate_spans):
            violations.append(
                ("pinned runtime path dereferenced outside the runtime "
                 "functions", line)
            )

    for name, pattern in FORBIDDEN_SYNTAX.items():
        for line in code:
            if re.search(pattern, line):
                violations.append((f"forbidden syntax: {name}", line))

    return violations


def test_sensitive_logic_uses_only_builtins_and_pinned_helpers():
    """Prove statically that lib/project-secrets.sh keeps its sensitive
    traversal, path, and identity logic on Bash builtins and the five
    pinned helpers: no ambient or GNU-only tool appears in any executable
    line; each helper tool name appears only in the pin table; no pinned
    helper field (PROJECT_SECRETS_HELPER_*) is dereferenced outside the
    require-helper/invoke-helper/pin-helpers/resolve-runtime gate bodies
    and the pinned runtime path (PROJECT_SECRETS_MSB_PATH) is
    dereferenced only inside the runtime functions, so every helper
    execution flows through the identity-revalidating invocation gate and
    the runtime executable runs only from the runtime functions; and no
    Bash syntax from the forbidden list is used.

    Runtime tests only cover the paths they exercise; a static scan pins
    the structural contract so an accidental ambient tool, a direct
    pinned-variable invocation, or newer syntax cannot silently break the
    Bash-4.0, builtins-only portability rule on the first host with a
    different toolset."""
    source = LIBRARY.read_text()
    code = _executable_lines(source)
    assert code
    assert _static_violations(source) == []

    # The invocation gate revalidates the pinned identity before invoking:
    # the require helper performs the identity check and the invoke helper
    # calls it before executing the pinned path.
    start, end = _function_body_span(code, "_project_secrets_require_helper")
    require_body = code[start:end]
    assert any(
        "_project_secrets_identity_matches" in line for line in require_body
    )
    start, end = _function_body_span(code, "_project_secrets_invoke_helper")
    invoke_body = code[start:end]
    gate = next(
        index for index, line in enumerate(invoke_body)
        if "_project_secrets_require_helper" in line
    )
    invoke = next(
        index for index, line in enumerate(invoke_body)
        if '"$path" "$@"' in line
    )
    assert gate < invoke


def test_static_scan_rejects_direct_pinned_variable_invocation():
    """Prove the static contract catches a direct invocation of a pinned
    helper or runtime variable outside the gates: the lowercase tool-word
    rule cannot see ``"$PROJECT_SECRETS_HELPER_MKTEMP"`` because the tool
    word is uppercase inside the variable name, so a separate rule must
    ban dereferencing the pinned state fields outside the gate function
    bodies.

    Without this rule a mutation could execute a pinned helper or the
    runtime directly, skipping the identity-revalidating invocation gate,
    while every other static and runtime test still passes."""
    source = LIBRARY.read_text()

    # A direct helper-variable invocation inside check_runtime, away from
    # every gate body.
    mutated = source.replace(
        "project_secrets_check_runtime() {\n",
        'project_secrets_check_runtime() {\n'
        '    "$PROJECT_SECRETS_HELPER_MKTEMP" -d /tmp/x\n',
        1,
    )
    assert mutated != source
    violations = _static_violations(mutated)
    assert any(
        rule == "pinned helper field dereferenced outside the gates"
        for rule, _ in violations
    )

    # A direct runtime-variable invocation inside an unrelated function.
    mutated = source.replace(
        "_project_secrets_fail() {\n",
        '_project_secrets_fail() {\n'
        '    "$PROJECT_SECRETS_MSB_PATH" --launch\n',
        1,
    )
    assert mutated != source
    violations = _static_violations(mutated)
    assert any(
        rule == "pinned runtime path dereferenced outside the runtime "
                "functions"
        for rule, _ in violations
    )
