"""Unit tests for run.sh using fake msb/podman binaries.

These tests prove run.sh generates the correct microsandbox invocation
(mounts, flags, env forwarding, image resolution, package approval flow)
without requiring KVM or a built image.
"""
import hashlib
import os
import pathlib
import pty
import re
import select
import shutil
import subprocess

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _fake_bin(tmp: pathlib.Path):
    """Install fake msb/podman into tmp/bin; return their log paths."""
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    msb_log = tmp / "msb.log"
    podman_log = tmp / "podman.log"
    images_file = tmp / "images.txt"
    msb = fake_bin / "msb"
    msb.write_text(FAKE_MSB)
    msb.chmod(0o755)
    podman = fake_bin / "podman"
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)
    return fake_bin, msb_log, podman_log, images_file


def _fake_env(tmp: pathlib.Path, cwd, fake_bin, msb_log, podman_log, images_file, env, home=None):
    fake_env = os.environ.copy()
    fake_env["PATH"] = f"{fake_bin}:{fake_env['PATH']}"
    # HOME follows the project dir so tests write ~/.env|~/.tau next to cwd;
    # secret-launch tests pass an explicit home hosting the project pair.
    fake_env["HOME"] = str(home) if home is not None else str(cwd or tmp)
    fake_env["MSB_LOG"] = str(msb_log)
    fake_env["PODMAN_LOG"] = str(podman_log)
    fake_env["MSB_IMAGES"] = str(images_file)
    fake_env.update(env or {})
    return fake_env


def _base_hash(root: pathlib.Path = REPO_ROOT) -> str:
    """Recompute run.sh's base-input hash: the SHA-256 of the hex digests
    of the Containerfile and every regular file under config/ (dotfiles
    included, bytewise path order), first 8 hex chars. Mirrors the run.sh
    pipeline so tests can predict per-project image tags."""
    config = root / "config"
    files = [root / "Containerfile"] + sorted(
        (p for p in config.iterdir() if p.is_file()),
        key=lambda p: p.name.encode(),
    )
    digests = "".join(hashlib.sha256(p.read_bytes()).hexdigest() for p in files)
    return hashlib.sha256(digests.encode("ascii")).hexdigest()[:8]


def _stub_repo(tmp: pathlib.Path, name: str, containerfile=True, config=True) -> pathlib.Path:
    """Copy run.sh and (optionally) Containerfile and config/ into a stub
    repo so tests can exercise run.sh's base-input handling without
    mutating the real repository files."""
    repo = tmp / name
    repo.mkdir()
    shutil.copy(REPO_ROOT / "run.sh", repo / "run.sh")
    shutil.copytree(REPO_ROOT / "lib", repo / "lib")
    if containerfile:
        shutil.copy(REPO_ROOT / "Containerfile", repo / "Containerfile")
    if config:
        shutil.copytree(REPO_ROOT / "config", repo / "config")
    return repo

FAKE_MSB = """#!/bin/bash
echo "msb $*" >> "$MSB_LOG"
if [ "$1" = "run" ] && [ -n "${MSB_ENV_LOG:-}" ]; then
    env > "$MSB_ENV_LOG"
fi
if [ "$1" = "run" ] && [ -n "${MSB_RUN_STATUS:-}" ]; then
    exit "$MSB_RUN_STATUS"
fi
if [ "$1" = "run" ] && [ -n "${MSB_SNAPSHOT_CHECK:-}" ]; then
    for arg in "$@"; do
        case "$arg" in
            *:/etc/tau-sandbox/bootstrap/tau/skills:ro)
                source="${arg%:/etc/tau-sandbox/bootstrap/tau/skills:ro}"
                if [ -f "$source/linked-skill/SKILL.md" ] && [ ! -L "$source/linked-skill" ]; then
                    printf 'dereferenced\\n' > "$MSB_SNAPSHOT_CHECK"
                fi
                ;;
        esac
    done
fi
if [ "$1" = "run" ] && [ -n "${MSB_SNAPSHOT_CONTENTS:-}" ]; then
    for arg in "$@"; do
        case "$arg" in
            *:/etc/tau-sandbox/bootstrap/tau/*:ro)
                # Record each snapshotted entry's name and contents so
                # tests can prove which config directory supplied it.
                source="${arg%%:/etc/tau-sandbox/bootstrap/tau/*:ro}"
                name="${arg##*/}"
                name="${name%:ro}"
                printf '%s\\t%s\\n' "$name" "$(cat "$source" 2>/dev/null)" >> "$MSB_SNAPSHOT_CONTENTS"
                ;;
        esac
    done
fi
case "$1" in
    images)
        if [ "$2" = "-q" ]; then
            [ -f "$MSB_IMAGES" ] && cat "$MSB_IMAGES"
        fi
        exit 0
        ;;
    volume) exit 0 ;;
    load) cat >/dev/null; exit 0 ;;
    rmi) [ "${MSB_RMI_FAIL:-0}" = "1" ] && exit 1 || exit 0 ;;
    *) exit 0 ;;
esac
"""

FAKE_PODMAN = """#!/bin/bash
echo "podman $*" >> "$PODMAN_LOG"
exit 0
"""


def invoke_run(*args, env=None, cwd=None, images=(), script=REPO_ROOT / "run.sh", home=None):
    """Run run.sh non-interactively (no TTY) with fake msb/podman.

    script selects which run.sh copy to execute; tests use a stub repo
    copy to exercise base-input handling without touching the real repo.
    home overrides $HOME (secret-launch tests host the project pair
    there). Returns (result, msb_log_lines, podman_log_lines).
    """
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tau-run-test-"))
    fake_bin, msb_log, podman_log, images_file = _fake_bin(tmp)
    images_file.write_text("\n".join(images) + ("\n" if images else ""))
    fake_env = _fake_env(tmp, cwd, fake_bin, msb_log, podman_log, images_file, env, home=home)

    result = subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        env=fake_env,
        cwd=str(cwd or tmp),
        # stdin must never be a terminal so package approvals always refuse
        # in the non-interactive path, no matter how pytest itself runs.
        stdin=subprocess.DEVNULL,
    )
    def _lines(path: pathlib.Path) -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    return result, _lines(msb_log), _lines(podman_log)


def invoke_run_tty(cwd, env=None, answer="y\n", images=(), script=REPO_ROOT / "run.sh", home=None):
    """Run run.sh in a pseudo-terminal and answer the package approval
    prompt. Returns (returncode, all_output).

    Needed because run.sh only builds per-project package images after an
    explicit interactive approval. home overrides $HOME.
    """
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tau-run-tty-"))
    fake_bin, msb_log, podman_log, images_file = _fake_bin(tmp)
    images_file.write_text("\n".join(images) + ("\n" if images else ""))
    fake_env = _fake_env(tmp, cwd, fake_bin, msb_log, podman_log, images_file, env, home=home)
    fake_env["TERM"] = "xterm"

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(script), "bash"],
        cwd=str(cwd),
        env=fake_env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)

    out = b""
    answered = False
    while proc.poll() is None:
        ready, _, _ = select.select([master], [], [], 0.2)
        if master not in ready:
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if not answered and b"Approve?" in out:
            os.write(master, answer.encode())
            answered = True

    os.close(master)
    proc.wait(timeout=60)

    def _lines(path: pathlib.Path) -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    return proc.returncode, out.decode(errors="replace"), _lines(msb_log), _lines(podman_log)


def test_run_script_exists_and_executable():
    script = REPO_ROOT / "run.sh"
    assert script.exists()
    assert os.access(script, os.X_OK)


def test_run_script_generates_correct_msb_command():
    """The core contract: project + volume mounts, resources, security
    profile, user identity, public network profile, env forwarding, and the
    ephemeral-run command shape `msb run ... IMAGE -- CMD`."""
    result, msb_log, _podman_log = invoke_run("bash")
    assert result.returncode == 0, f"stderr: {result.stderr}"

    run_line = next(line for line in msb_log if line.startswith("msb run"))
    # Mounts: workspace plus isolated home, session, and log volumes.
    assert "-v " in run_line and ":/workspace" in run_line
    persist_token = next(tok for tok in run_line.split() if tok.startswith("tau-persist-"))
    assert "tau-persist-tau-run-test-" in persist_token
    assert persist_token.endswith(":/home/tau")
    assert "tau-sessions-tau-run-test-" in run_line
    assert ":/var/lib/tau-sandbox/sessions" in run_line
    assert "tau-logs-tau-run-test-" in run_line
    assert ":/var/lib/tau-sandbox/logs" in run_line
    assert ":/home/tau/.tau/sessions" not in run_line
    assert ":/home/tau/.tau/logs" not in run_line
    assert "/config/APPEND_SYSTEM.md:/etc/tau-sandbox/APPEND_SYSTEM.md:ro" in run_line
    # Resources and limits
    assert "-c 4" in run_line
    assert "-m 8G" in run_line
    assert "--rlimit nproc=1024" in run_line
    # Security posture
    assert "--security restricted" in run_line
    assert "--tmpfs /tmp" in run_line
    assert "--user 1000:1000" in run_line
    # Public profile: internet egress + gateway DNS from msb, LAN exceptions
    # only when TAU_LAN_HOSTS is set (empty by default), and no published
    # inbound ports. The old --net-default-ingress deny path dropped DNS,
    # so it must not come back.
    assert "--net public" in run_line
    assert "--net-rule" not in run_line
    assert "--net private" not in run_line
    assert "--net-default-ingress" not in run_line
    # Working directory
    assert "-w /workspace" in run_line
    # Image and command
    assert "localhost/tau-agent-isolated:latest -- bash" in run_line


def test_lan_hosts_emit_one_net_rule_per_entry(tmp_path):
    """TAU_LAN_HOSTS adds one exact-IP --net-rule argument per entry."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run(
        "bash", cwd=tmp_path, env={"TAU_LAN_HOSTS": "192.168.1.100,192.168.1.101"}
    )
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "--net-rule allow@192.168.1.100" in run_line
    assert "--net-rule allow@192.168.1.101" in run_line


def test_run_script_uses_entrypoint_from_image():
    """run.sh must NOT append /usr/local/bin/entrypoint.sh: the image
    ENTRYPOINT already runs it (msb preserves ENTRYPOINT for `-- CMD`).
    The user command is passed through verbatim."""
    result, msb_log, _ = invoke_run("tau", "-p", "hello")
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert run_line.endswith("-- tau -p hello")
    assert "entrypoint.sh" not in run_line


def test_run_script_forwards_env_file(tmp_path):
    """API keys defined in ~/.env reach the sandbox as -e KEY=VALUE args,
    and are never echoed to stderr."""
    (tmp_path / ".env").write_text(
        "VLLM_API_KEY=test-vllm-key\nOPENROUTER_API_KEY=test-or-key\n"
    )
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "-e VLLM_API_KEY=test-vllm-key" in run_line
    assert "-e OPENROUTER_API_KEY=test-or-key" in run_line
    assert "test-vllm-key" not in result.stderr


def test_run_script_mounts_host_config_as_bootstrap_without_shared_credentials(tmp_path):
    """Host config is a read-only bootstrap source; the host credential
    file is neither mounted nor bootstrapped, so each sandbox uses
    project-local credentials."""
    (tmp_path / ".env").write_text("")
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    (tau_dir / "skills").mkdir()
    (tau_dir / "settings.json").write_text("{}\n")
    (tau_dir / "credentials.json").write_text('{"openai": "sk-host"}\n')
    (tau_dir / "sessions").mkdir()
    (tau_dir / "logs").mkdir()
    (tau_dir / "trust.json").write_text('{"version": 1, "decisions": []}\n')
    (tau_dir / "trust.json.lock").write_text("")
    (tmp_path / ".agents").mkdir()

    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    bootstrap = "/etc/tau-sandbox/bootstrap/tau"
    assert f":{bootstrap}/skills:ro" in run_line
    assert f":{bootstrap}/settings.json:ro" in run_line
    assert f"-v {tau_dir.resolve()}/skills:{bootstrap}/skills:ro" not in run_line
    assert f"-v {tau_dir.resolve()}/settings.json:{bootstrap}/settings.json:ro" not in run_line
    assert f"{tau_dir.resolve()}/settings.json:/home/tau/.tau/settings.json" not in run_line
    # credentials.json appears nowhere in the invocation: no shared mount,
    # no bootstrap snapshot of it, no direct mount into the home.
    assert "credentials.json" not in run_line
    assert f"-v {tau_dir.resolve()}/sessions" not in run_line
    assert f"-v {tau_dir.resolve()}/logs" not in run_line
    assert f"-v {tau_dir.resolve()}/trust.json" not in run_line
    assert f"-v {tau_dir.resolve()}/trust.json.lock" not in run_line
    assert f"-v {tmp_path.resolve()}/.agents:/home/tau/.agents:ro" in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=0" in run_line


def test_run_script_follows_host_config_symlinks(tmp_path):
    """Linked config resources are mounted by target under their link names."""
    (tmp_path / ".env").write_text("")
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    targets = tmp_path / "config-targets"
    targets.mkdir()
    skills = targets / "skills"
    skills.mkdir()
    skill_source = targets / "skill-source"
    skill_source.mkdir()
    (skill_source / "SKILL.md").write_text("# linked skill\n")
    (skills / "linked-skill").symlink_to(skill_source, target_is_directory=True)
    extensions = targets / "extensions"
    extensions.mkdir()
    settings = targets / "settings.json"
    settings.write_text("{}\n")
    (tau_dir / "skills").symlink_to(skills, target_is_directory=True)
    (tau_dir / "extensions").symlink_to(extensions, target_is_directory=True)
    (tau_dir / "settings.json").symlink_to(settings)

    snapshot_check = tmp_path / "snapshot-check"
    result, msb_log, _ = invoke_run(
        "bash", cwd=tmp_path, env={"MSB_SNAPSHOT_CHECK": str(snapshot_check)}
    )
    assert result.returncode == 0
    assert snapshot_check.read_text() == "dereferenced\n"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    bootstrap = "/etc/tau-sandbox/bootstrap/tau"
    assert f":{bootstrap}/skills:ro" in run_line
    assert f":{bootstrap}/extensions:ro" in run_line
    assert f":{bootstrap}/settings.json:ro" in run_line
    assert str(skills.resolve()) not in run_line
    assert str(skill_source.resolve()) not in run_line
    assert str(extensions.resolve()) not in run_line
    assert str(settings.resolve()) not in run_line
    assert "tau-sandbox-bootstrap." in run_line


def test_run_script_skips_missing_host_config_mounts(tmp_path):
    """Absent host config leaves Tau state local to the persistent home;
    no host credential file is mounted either."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"{tmp_path.resolve()}/.tau" not in run_line
    assert f"{tmp_path.resolve()}/.agents" not in run_line
    assert "credentials.json" not in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=0" in run_line
    assert ":/var/lib/tau-sandbox/sessions" in run_line
    assert ":/var/lib/tau-sandbox/logs" in run_line


def test_run_script_discovers_nearest_ancestor_tau_config(tmp_path):
    """With TAU_CONFIG_DIR unset, the closest ancestor's .tau directory
    supplies the host config; the snapshotted settings.json proves which
    dir won and credentials.json is never mounted."""
    (tmp_path / ".env").write_text("")
    project = tmp_path / "project"
    workdir = project / "nested" / "dir"
    workdir.mkdir(parents=True)
    tau = project / ".tau"
    tau.mkdir()
    (tau / "settings.json").write_text('{"scope": "project"}\n')
    (tau / "credentials.json").write_text('{"openai": "sk-project"}\n')

    snapshot_contents = tmp_path / "snapshot-contents"
    result, msb_log, _ = invoke_run(
        "bash", cwd=workdir,
        env={"MSB_SNAPSHOT_CONTENTS": str(snapshot_contents)},
    )
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "credentials.json" not in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=0" in run_line
    assert 'settings.json\t{"scope": "project"}' in snapshot_contents.read_text()


def test_run_script_innermost_ancestor_tau_config_wins(tmp_path):
    """Nested project configs shadow outer ones: closest ancestor wins, so
    a repo inside a configured project can override the project config."""
    (tmp_path / ".env").write_text("")
    project = tmp_path / "project"
    workdir = project / "inner" / "sub"
    workdir.mkdir(parents=True)
    outer_tau = project / ".tau"
    inner_tau = project / "inner" / ".tau"
    outer_tau.mkdir()
    inner_tau.mkdir()
    (outer_tau / "settings.json").write_text('{"scope": "outer"}\n')
    (outer_tau / "credentials.json").write_text('{"openai": "sk-outer"}\n')
    (inner_tau / "settings.json").write_text('{"scope": "inner"}\n')
    (inner_tau / "credentials.json").write_text('{"openai": "sk-inner"}\n')

    snapshot_contents = tmp_path / "snapshot-contents"
    result, msb_log, _ = invoke_run(
        "bash", cwd=workdir,
        env={"MSB_SNAPSHOT_CONTENTS": str(snapshot_contents)},
    )
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "credentials.json" not in run_line
    snapshot = snapshot_contents.read_text()
    assert 'settings.json\t{"scope": "inner"}' in snapshot
    assert '{"scope": "outer"}' not in snapshot


def test_run_script_discovers_tau_config_through_root_symlink(tmp_path):
    """A project-root .tau symlink to an external config world is followed:
    the world's real paths appear, so secrets can live outside the tree."""
    (tmp_path / ".env").write_text("")
    project = tmp_path / "project"
    workdir = project / "src"
    workdir.mkdir(parents=True)
    world = tmp_path / "config-world"
    world.mkdir()
    (world / "settings.json").write_text('{"scope": "world"}\n')
    (world / "credentials.json").write_text('{"openai": "sk-world"}\n')
    (project / ".tau").symlink_to(world, target_is_directory=True)

    snapshot_contents = tmp_path / "snapshot-contents"
    result, msb_log, _ = invoke_run(
        "bash", cwd=workdir,
        env={"MSB_SNAPSHOT_CONTENTS": str(snapshot_contents)},
    )
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "credentials.json" not in run_line
    assert 'settings.json\t{"scope": "world"}' in snapshot_contents.read_text()


def test_run_script_tau_config_dir_override_beats_discovery(tmp_path):
    """An explicitly set TAU_CONFIG_DIR always wins over a discovered .tau,
    preserving the documented override for tests and automation."""
    (tmp_path / ".env").write_text("")
    project = tmp_path / "project"
    workdir = project / "src"
    workdir.mkdir(parents=True)
    (project / ".tau").mkdir()
    override = tmp_path / "override"
    override.mkdir()
    (override / "settings.json").write_text('{"scope": "override"}\n')
    (override / "credentials.json").write_text('{"openai": "sk-override"}\n')

    snapshot_contents = tmp_path / "snapshot-contents"
    result, msb_log, _ = invoke_run(
        "bash", cwd=workdir,
        env={"TAU_CONFIG_DIR": str(override),
             "MSB_SNAPSHOT_CONTENTS": str(snapshot_contents)},
    )
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "credentials.json" not in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=0" in run_line
    assert 'settings.json\t{"scope": "override"}' in snapshot_contents.read_text()


def test_run_script_dangling_tau_symlink_falls_back_to_default(tmp_path):
    """A dangling .tau link is not a config directory, so discovery keeps
    walking and the default config applies instead of aborting."""
    (tmp_path / ".env").write_text("")
    project = tmp_path / "project"
    workdir = project / "src"
    workdir.mkdir(parents=True)
    (project / ".tau").symlink_to(
        tmp_path / "missing-world", target_is_directory=True
    )

    result, msb_log, _ = invoke_run("bash", cwd=workdir)
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=0" in run_line


def test_run_script_resources_are_overridable(tmp_path):
    """TAU_CPUS / TAU_MEM / TAU_PIDS override the defaults."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run(
        "bash", cwd=tmp_path, env={"TAU_CPUS": "2", "TAU_MEM": "4G", "TAU_PIDS": "512"}
    )
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "-c 2" in run_line
    assert "-m 4G" in run_line
    assert "--rlimit nproc=512" in run_line


def test_per_project_image_with_packages_builds_and_loads(tmp_path):
    """.tau-packages triggers an interactive approval, then a per-project
    image name and a podman build --build-arg EXTRA_PACKAGES + save | msb
    load pipeline."""
    pkg_file = tmp_path / ".tau-packages"
    pkg_file.write_text("# build tools\ncmake\npkgconf\n")
    pkg_hash = hashlib.sha256(pkg_file.read_bytes()).hexdigest()[:8]
    (tmp_path / ".env").write_text("")

    rc, output, msb_log, podman_log = invoke_run_tty(cwd=tmp_path, answer="y\n")
    assert rc == 0, f"output: {output}"
    assert "Approve?" in output

    expected_image = f"tau-agent-isolated-{tmp_path.name}-{_base_hash()}-{pkg_hash}"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"localhost/{expected_image}:latest -- bash" in run_line
    build_line = next(line for line in podman_log if line.startswith("podman build"))
    assert "--build-arg EXTRA_PACKAGES=cmake pkgconf" in build_line
    assert "-t" in build_line and expected_image in build_line
    save_line = next(line for line in podman_log if line.startswith("podman save"))
    assert expected_image in save_line


def test_dot_directory_package_image_name_is_sanitized(tmp_path):
    """A dot-directory basename is illegal in an OCI image reference (the
    "-." sequence violates the path-component grammar), so the per-project
    image name must drop the leading dot while the volume names keep the
    raw basename: dot-directory volume names are legal msb volume names and
    may already hold persistent state."""
    project = tmp_path / ".dotproj"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    pkg_hash = hashlib.sha256((project / ".tau-packages").read_bytes()).hexdigest()[:8]

    rc, output, msb_log, podman_log = invoke_run_tty(cwd=project, answer="y\n")
    assert rc == 0, f"output: {output}"
    assert "Approve?" in output

    expected_image = f"tau-agent-isolated-dotproj-{_base_hash()}-{pkg_hash}"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"localhost/{expected_image}:latest -- bash" in run_line
    build_line = next(line for line in podman_log if line.startswith("podman build"))
    assert "-t" in build_line and expected_image in build_line
    # Volume names keep the raw basename (legal msb volume names).
    assert "tau-persist-.dotproj-" in run_line
    assert "tau-sessions-.dotproj-" in run_line
    assert "tau-logs-.dotproj-" in run_line


def test_dot_directory_without_packages_keeps_raw_volume_names(tmp_path):
    """A dot directory without packages boots the shared base image and its
    volume names keep the raw basename, pinning the working no-package path
    for dot directories."""
    project = tmp_path / ".dotproj"
    project.mkdir()
    (project / ".env").write_text("")

    result, msb_log, _ = invoke_run("bash", cwd=project)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "localhost/tau-agent-isolated:latest -- bash" in run_line
    assert "tau-persist-.dotproj-" in run_line
    assert "tau-sessions-.dotproj-" in run_line
    assert "tau-logs-.dotproj-" in run_line


def test_uppercase_directory_image_name_is_lowercased(tmp_path):
    """Uppercase basenames are illegal in OCI references: the image name is
    lowercased while the volume name keeps the raw (legal) basename, so
    existing state of working uppercase-named projects stays put."""
    project = tmp_path / "MyProject"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    pkg_hash = hashlib.sha256((project / ".tau-packages").read_bytes()).hexdigest()[:8]

    rc, output, msb_log, _ = invoke_run_tty(cwd=project, answer="y\n")
    assert rc == 0, f"output: {output}"

    expected_image = f"tau-agent-isolated-myproject-{_base_hash()}-{pkg_hash}"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"localhost/{expected_image}:latest -- bash" in run_line
    assert "tau-persist-MyProject-" in run_line


def test_space_directory_names_are_sanitized(tmp_path):
    """A space is illegal in both msb volume names and OCI references, so
    both derived name families use the sanitized basename."""
    project = tmp_path / "my project"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    pkg_hash = hashlib.sha256((project / ".tau-packages").read_bytes()).hexdigest()[:8]

    rc, output, msb_log, _ = invoke_run_tty(cwd=project, answer="y\n")
    assert rc == 0, f"output: {output}"

    expected_image = f"tau-agent-isolated-my_project-{_base_hash()}-{pkg_hash}"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"localhost/{expected_image}:latest -- bash" in run_line
    assert "tau-persist-my_project-" in run_line
    assert "tau-sessions-my_project-" in run_line
    assert "tau-logs-my_project-" in run_line


def test_dot_directory_rebuild_prunes_superseded_images(tmp_path):
    """Pruning must match the sanitized image name: the legacy and stale
    base tags of the dot-directory project's package content are removed
    while other package hashes (same-image-name sibling) are kept."""
    project = tmp_path / ".dotproj"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    pkg_hash = hashlib.sha256((project / ".tau-packages").read_bytes()).hexdigest()[:8]
    name = "dotproj"  # sanitized image name
    legacy_current = f"localhost/tau-agent-isolated-{name}-{pkg_hash}:latest"
    stale_base = f"localhost/tau-agent-isolated-{name}-00000000-{pkg_hash}:latest"
    sibling = f"localhost/tau-agent-isolated-{name}-deadbeef-ffffffff:latest"

    rc, output, msb_log, _ = invoke_run_tty(
        cwd=project, answer="y\n", images=(legacy_current, stale_base, sibling),
    )
    assert rc == 0, f"output: {output}"
    current = f"localhost/tau-agent-isolated-{name}-{_base_hash()}-{pkg_hash}:latest"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"{current} -- bash" in run_line
    rmi_lines = {line for line in msb_log if line.startswith("msb rmi")}
    assert rmi_lines == {f"msb rmi {legacy_current}", f"msb rmi {stale_base}"}


def test_current_package_image_skips_build_and_prune(tmp_path):
    """An up-to-date cached package image (current base and package hashes)
    boots directly: no podman build, no rmi. Locks the reuse guarantee and
    the no-prune-on-cache-hit rule."""
    (tmp_path / ".tau-packages").write_text("cmake\n")
    (tmp_path / ".env").write_text("")
    pkg_hash = hashlib.sha256((tmp_path / ".tau-packages").read_bytes()).hexdigest()[:8]
    current = f"localhost/tau-agent-isolated-{tmp_path.name}-{_base_hash()}-{pkg_hash}:latest"
    result, msb_log, podman_log = invoke_run("bash", cwd=tmp_path, images=(current,))
    assert result.returncode == 0
    assert not podman_log, f"unexpected podman invocations: {podman_log}"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"{current} -- bash" in run_line
    assert not any(line.startswith("msb rmi") for line in msb_log)


def test_missing_containerfile_aborts_package_launch(tmp_path):
    """A package project cannot derive its image tag without the
    Containerfile: the clone is broken, and launching with a tag whose
    freshness cannot be verified would silently pin an old base."""
    repo = _stub_repo(tmp_path, "stub-repo", containerfile=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    result, _, podman_log = invoke_run("bash", cwd=project, script=repo / "run.sh")
    assert result.returncode == 1
    assert "Containerfile" in result.stderr
    assert not podman_log


def test_missing_config_aborts_package_launch(tmp_path):
    """A package project cannot derive its image tag without the config/
    directory, for the same freshness reason as a missing Containerfile."""
    repo = _stub_repo(tmp_path, "stub-repo", config=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    result, _, podman_log = invoke_run("bash", cwd=project, script=repo / "run.sh")
    assert result.returncode == 1
    assert "config" in result.stderr
    assert not podman_log


def test_added_base_input_changes_tag(tmp_path):
    """Adding a regular file under config/ changes the derived tag (set-
    change semantics). The same project is launched against two stub repos
    that differ only by an extra config file, so only the base hash can
    differ between the two tags."""
    base_repo = _stub_repo(tmp_path, "stub-a")
    extra_repo = _stub_repo(tmp_path, "stub-b")
    (extra_repo / "config" / "extra.txt").write_text("extra\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    rc_a, _, msb_a, _ = invoke_run_tty(cwd=project, script=base_repo / "run.sh", answer="y\n")
    rc_b, _, msb_b, _ = invoke_run_tty(cwd=project, script=extra_repo / "run.sh", answer="y\n")
    rc_c, _, msb_c, _ = invoke_run_tty(cwd=project, script=base_repo / "run.sh", answer="y\n")
    assert rc_a == 0 and rc_b == 0 and rc_c == 0

    def tag(msb_log):
        # The image ref is the last token before the ` -- ` separator.
        run_line = next(line for line in msb_log if line.startswith("msb run"))
        return run_line.rsplit(" -- ", 1)[0].rsplit(" ", 1)[1]

    tag_a, tag_b, tag_c = tag(msb_a), tag(msb_b), tag(msb_c)
    # Addition changes the tag; removal restores it (set-change semantics).
    assert tag_a != tag_b
    assert tag_c == tag_a
    assert _base_hash(base_repo) in tag_a
    assert _base_hash(extra_repo) in tag_b
    assert _base_hash(base_repo) != _base_hash(extra_repo)
    # Content-change branch: rewriting an existing input's bytes (not just
    # the file set) must also change the tag.
    content_file = base_repo / "config" / "APPEND_SYSTEM.md"
    content_file.write_bytes(content_file.read_bytes() + b"\n# base update\n")
    rc_d, _, msb_d, _ = invoke_run_tty(cwd=project, script=base_repo / "run.sh", answer="y\n")
    assert rc_d == 0
    tag_d = tag(msb_d)
    assert tag_d != tag_a
    assert _base_hash(base_repo) in tag_d


def test_non_file_config_entries_do_not_change_tag(tmp_path):
    """A directory inside config/ must not change the derived tag: the hash
    input set is regular files only, so an incidental directory (e.g. a
    build byproduct) keeps the cached image usable. Pins the scenario
    deterministically instead of relying on __pycache__ happening to sit
    in the working tree's config/."""
    repo = _stub_repo(tmp_path, "stub-repo")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    pkg_hash = hashlib.sha256((project / ".tau-packages").read_bytes()).hexdigest()[:8]
    current = (
        f"localhost/tau-agent-isolated-{project.name}-{_base_hash(repo)}-{pkg_hash}:latest"
    )
    # A directory appears under config/ after the image was built.
    (repo / "config" / "build-cache").mkdir()
    result, msb_log, podman_log = invoke_run(
        "bash", cwd=project, script=repo / "run.sh", images=(current,)
    )
    assert result.returncode == 0
    assert not podman_log
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"{current} -- bash" in run_line


def test_stale_package_image_rebuilds_and_prunes_superseded(tmp_path):
    """A base change invalidates the per-project tag: the cached legacy and
    superseded images of the current package content are replaced after
    approval, and pruned afterwards, while images with other package hashes
    (same-basename sibling, earlier content) remain untouched."""
    (tmp_path / ".tau-packages").write_text("cmake\n")
    (tmp_path / ".env").write_text("")
    pkg_hash = hashlib.sha256((tmp_path / ".tau-packages").read_bytes()).hexdigest()[:8]
    name = tmp_path.name
    legacy_current = f"localhost/tau-agent-isolated-{name}-{pkg_hash}:latest"
    stale_base = f"localhost/tau-agent-isolated-{name}-00000000-{pkg_hash}:latest"
    sibling = f"localhost/tau-agent-isolated-{name}-deadbeef-ffffffff:latest"
    legacy_other = f"localhost/tau-agent-isolated-{name}-ffffffff:latest"

    rc, output, msb_log, podman_log = invoke_run_tty(
        cwd=tmp_path, answer="y\n",
        images=(legacy_current, stale_base, sibling, legacy_other),
    )
    assert rc == 0, f"output: {output}"
    assert "Approve?" in output
    current = f"localhost/tau-agent-isolated-{name}-{_base_hash()}-{pkg_hash}:latest"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"{current} -- bash" in run_line
    build_line = next(line for line in podman_log if line.startswith("podman build"))
    # podman build tags the host image without localhost/ and without :latest.
    assert current.removeprefix("localhost/").removesuffix(":latest") in build_line
    rmi_lines = {line for line in msb_log if line.startswith("msb rmi")}
    assert rmi_lines == {f"msb rmi {legacy_current}", f"msb rmi {stale_base}"}


def test_stale_package_image_refuses_non_interactively(tmp_path):
    """A base-change rebuild keeps the approval gate: with only a stale tag
    in the cache and no terminal, the launch fails without building."""
    (tmp_path / ".tau-packages").write_text("cmake\n")
    (tmp_path / ".env").write_text("")
    stale = f"localhost/tau-agent-isolated-{tmp_path.name}-00000000:latest"
    result, _, podman_log = invoke_run("bash", cwd=tmp_path, images=(stale,))
    assert result.returncode == 1
    assert "not a terminal" in result.stderr
    assert not podman_log


def test_prune_rmi_failure_does_not_fail_launch(tmp_path):
    """A failed msb rmi during pruning must never fail the build, load, or
    launch, and must not be reported as an error: pruning is cache hygiene,
    not a launch blocker."""
    (tmp_path / ".tau-packages").write_text("cmake\n")
    (tmp_path / ".env").write_text("")
    pkg_hash = hashlib.sha256((tmp_path / ".tau-packages").read_bytes()).hexdigest()[:8]
    stale = f"localhost/tau-agent-isolated-{tmp_path.name}-00000000-{pkg_hash}:latest"
    rc, output, _, podman_log = invoke_run_tty(
        cwd=tmp_path, answer="y\n", images=(stale,), env={"MSB_RMI_FAIL": "1"},
    )
    assert rc == 0, f"output: {output}"
    # run.sh silences msb rmi output; nothing may leak into the session.
    assert "Error" not in output
    assert any(line.startswith("podman build") for line in podman_log)


def test_shared_base_launch_ignores_missing_base_inputs(tmp_path):
    """Projects without a non-empty .tau-packages file never derive a
    package tag, so a missing Containerfile must not abort them: they boot
    the shared base image."""
    repo = _stub_repo(tmp_path, "stub-repo", containerfile=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=project, script=repo / "run.sh")
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "localhost/tau-agent-isolated:latest -- bash" in run_line


def test_tau_image_override_ignores_missing_base_inputs(tmp_path):
    """TAU_IMAGE bypasses package processing entirely, so a missing
    Containerfile must not abort an override launch."""
    repo = _stub_repo(tmp_path, "stub-repo", containerfile=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tau-packages").write_text("cmake\n")
    (project / ".env").write_text("")
    result, msb_log, _ = invoke_run(
        "bash", cwd=project, script=repo / "run.sh", env={"TAU_IMAGE": "custom:tag"}
    )
    assert result.returncode == 0
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "custom:tag -- bash" in run_line


def test_packages_approval_declined_aborts(tmp_path):
    """Declining the approval aborts without building anything."""
    (tmp_path / ".tau-packages").write_text("cmake\n")
    (tmp_path / ".env").write_text("")
    rc, output, _, podman_log = invoke_run_tty(cwd=tmp_path, answer="n\n")
    assert rc == 1
    assert "Aborted" in output
    assert not podman_log


def test_no_rebuild_when_image_exists(tmp_path):
    """Image presence in the msb cache skips podman build entirely."""
    (tmp_path / ".env").write_text("")
    result, msb_log, podman_log = invoke_run(
        "bash", cwd=tmp_path, images=("localhost/tau-agent-isolated:latest",)
    )
    assert result.returncode == 0
    assert not podman_log, f"unexpected podman invocations: {podman_log}"
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "localhost/tau-agent-isolated:latest -- bash" in run_line


def test_packages_approval_required_interactively(tmp_path):
    """Package rebuilds refuse when stdin is not a terminal."""
    (tmp_path / ".tau-packages").write_text("cmake\n")
    (tmp_path / ".env").write_text("")
    result, _, _ = invoke_run("bash", cwd=tmp_path)
    assert result.returncode == 1
    assert "not a terminal" in result.stderr


def test_packages_reject_dangerous_characters(tmp_path):
    """Shell metacharacters in .tau-packages abort before any build."""
    (tmp_path / ".tau-packages").write_text("cmake; rm -rf /\n")
    (tmp_path / ".env").write_text("")
    result, _, podman_log = invoke_run("bash", cwd=tmp_path)
    assert result.returncode == 1
    assert "dangerous characters" in result.stderr
    assert not podman_log


def test_tau_image_override_bypasses_packages(tmp_path):
    """TAU_IMAGE passes the reference through and never builds."""
    (tmp_path / ".tau-packages").write_text("cmake\n")
    (tmp_path / ".env").write_text("")
    result, msb_log, podman_log = invoke_run(
        "bash", cwd=tmp_path, env={"TAU_IMAGE": "custom:tag"}
    )
    assert result.returncode == 0
    assert not podman_log
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "custom:tag -- bash" in run_line


def test_reset_removes_all_project_volumes(tmp_path):
    """--reset removes home, session, and log volumes together."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("--reset", cwd=tmp_path)
    assert result.returncode == 0
    assert "Volumes tau-persist-" in result.stdout
    rm_line = next(line for line in msb_log if "volume rm" in line)
    assert f"msb volume rm tau-persist-{tmp_path.name}-" in rm_line
    assert f"tau-sessions-{tmp_path.name}-" in rm_line
    assert f"tau-logs-{tmp_path.name}-" in rm_line


def test_shared_base_image_used_without_packages(tmp_path):
    """No .tau-packages => the shared base image reference is used."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "localhost/tau-agent-isolated:latest -- bash" in run_line


# --- Project-secret integration (present-pair launches through run.sh) ---
#
# These tests prove run.sh integrates lib/project-secrets.sh: the exact
# launch-directory mapping, the pair and reserved-name sanity checks, the
# sourced values reaching the runtime environment, forwarding suppression,
# and exactly one pass-through --secret-conf in the final invocation.

BASE_IMAGE = "localhost/tau-agent-isolated:latest"
DUMMY_VALUE = "dummy-value-123"
DEFAULT_YAML = "KEY:\n  value: \"${KEY}\"\n  allow:\n    - api.example.com\n"


def make_secret_project(tmp_path, project="proj", env_text=None, yaml_text=None):
    """A present-pair home: HOME/Projects/<project> is the launch directory
    and HOME/.<project> holds the exact mapped source pair."""
    home = tmp_path / "home"
    proj = home / "Projects" / project
    proj.mkdir(parents=True, exist_ok=True)
    secret = home / f".{project}"
    secret.mkdir(exist_ok=True)
    (secret / "secrets.env").write_text(
        env_text if env_text is not None else f"KEY={DUMMY_VALUE}\n"
    )
    (secret / "secrets.yaml").write_text(
        yaml_text if yaml_text is not None else DEFAULT_YAML
    )
    return home, proj, secret


def _secret_run_line(msb_log):
    """The final runtime invocation of a present-pair launch: run.sh passes
    --secret-conf immediately after `run`, so its presence is the
    observable marker of an active pair."""
    return next((line for line in msb_log if line.startswith("msb run --secret-conf")), None)


def test_reset_bypasses_invalid_secret_sources(tmp_path):
    """--reset removes the per-project volumes and exits without any
    project-secret work: invalid secret sources must not block it and no
    secret file may be read. This pins the reset-before-discovery
    ordering: a user must always be able to clean up even when the pair
    is broken."""
    home, proj, secret = make_secret_project(tmp_path)
    (secret / "secrets.env").unlink()
    (secret / "secrets.env").mkdir()  # invalid source type
    result, msb_log, podman_log = invoke_run("--reset", cwd=proj, home=home)
    assert result.returncode == 0
    assert "Volumes tau-persist-proj-" in result.stdout
    assert len(msb_log) == 1
    assert msb_log[0].startswith("msb volume rm tau-persist-proj-")
    assert "tau-sessions-proj-" in msb_log[0]
    assert "tau-logs-proj-" in msb_log[0]
    assert not podman_log


def test_root_outside_relative_and_nested_mapping_contract(tmp_path):
    """Every exact mapping scenario through the launcher: the default root,
    exact nested mapping without inheritance, explicit absolute and relative
    TAU_PROJECTS_DIR roots, launches outside the root, an absent default
    root, and the invalid explicitly-empty override. The --secret-conf
    marker distinguishes present pairs from secret-free launches."""
    home, proj, secret = make_secret_project(tmp_path)
    # Default root: the exact hidden home directory maps the project.
    result, msb_log, _ = invoke_run("bash", cwd=proj, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 0, result.stderr
    assert _secret_run_line(msb_log) is not None
    # A nested launch does not inherit: only the exact nested pair counts.
    nested = proj / "sub"
    nested.mkdir()
    result, msb_log, _ = invoke_run("bash", cwd=nested, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 0, result.stderr
    assert _secret_run_line(msb_log) is None
    # The exact nested hidden directory tree maps the nested launch.
    nested_secret = home / ".proj" / "sub"
    nested_secret.mkdir(parents=True)
    (nested_secret / "secrets.env").write_text(f"KEY={DUMMY_VALUE}\n")
    (nested_secret / "secrets.yaml").write_text(DEFAULT_YAML)
    result, msb_log, _ = invoke_run("bash", cwd=nested, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 0, result.stderr
    assert _secret_run_line(msb_log) is not None
    # An explicit absolute root changes the mapping.
    root2 = tmp_path / "root2"
    (root2 / "proj").mkdir(parents=True)
    result, msb_log, _ = invoke_run(
        "bash", cwd=root2 / "proj", home=home, images=(BASE_IMAGE,),
        env={"TAU_PROJECTS_DIR": str(root2)},
    )
    assert result.returncode == 0, result.stderr
    assert _secret_run_line(msb_log) is not None
    # A relative explicit root resolves from the launch directory.
    root3 = tmp_path / "root3"
    (root3 / "proj").mkdir(parents=True)
    result, msb_log, _ = invoke_run(
        "bash", cwd=root3 / "proj", home=home, images=(BASE_IMAGE,),
        env={"TAU_PROJECTS_DIR": ".."},
    )
    assert result.returncode == 0, result.stderr
    assert _secret_run_line(msb_log) is not None
    # A launch outside the root has no secrets.
    outside = home / "work"
    outside.mkdir()
    result, msb_log, _ = invoke_run("bash", cwd=outside, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 0, result.stderr
    assert _secret_run_line(msb_log) is None
    # An absent default root disables discovery.
    home2 = tmp_path / "home2"
    (home2 / "work").mkdir(parents=True)
    result, msb_log, _ = invoke_run(
        "bash", cwd=home2 / "work", home=home2, images=(BASE_IMAGE,)
    )
    assert result.returncode == 0, result.stderr
    assert _secret_run_line(msb_log) is None
    # An explicitly empty TAU_PROJECTS_DIR is invalid.
    result, msb_log, _ = invoke_run(
        "bash", cwd=proj, home=home, images=(BASE_IMAGE,),
        env={"TAU_PROJECTS_DIR": ""},
    )
    assert result.returncode == 1
    assert "TAU_PROJECTS_DIR" in result.stderr
    assert not any(line.startswith("msb run") for line in msb_log)


def test_no_pair_preserves_existing_invocation(tmp_path):
    """A no-pair launch keeps the exact existing msb invocation shape and
    applies no secret configuration: no --secret-conf anywhere, ordinary
    forwarding unchanged (regression)."""
    home, proj, secret = make_secret_project(tmp_path)
    outside = home / "work"
    outside.mkdir()
    (home / ".env").write_text("ORD=1\n")
    result, msb_log, _ = invoke_run(
        "bash", cwd=outside, home=home, images=(BASE_IMAGE,),
    )
    assert result.returncode == 0, result.stderr
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert "--secret-conf" not in run_line
    assert "-e ORD=1" in run_line
    assert "localhost/tau-agent-isolated:latest -- bash" in run_line


def test_incomplete_pair_and_invalid_derived_directory_fail(tmp_path):
    """A half-configured pair or a broken secret directory aborts the
    launch before any runtime call, naming the offending path: silent
    fallback to unprotected launching would hide a configuration error."""
    home, proj, secret = make_secret_project(tmp_path)
    (secret / "secrets.env").unlink()
    result, msb_log, podman_log = invoke_run("bash", cwd=proj, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 1
    assert "secrets.env" in result.stderr
    assert not any(line.startswith("msb run") for line in msb_log)
    assert not podman_log
    # Broken derived directory (plain file) also fails before any run.
    home2, proj2, secret2 = make_secret_project(tmp_path / "second")
    import shutil
    shutil.rmtree(secret2)
    secret2.write_text("not a directory")
    result, msb_log, _ = invoke_run("bash", cwd=proj2, home=home2, images=(BASE_IMAGE,))
    assert result.returncode == 1
    assert "secret directory" in result.stderr
    assert not any(line.startswith("msb run") for line in msb_log)


def test_reserved_secret_name_fails_before_boot(tmp_path):
    """A declared reserved name (exact set or BASH/TAU_ prefix) aborts the
    launch before any runtime call, naming the variable: such a secret
    would let the runtime overwrite shell- or launcher-critical state."""
    home, proj, secret = make_secret_project(
        tmp_path, env_text=f"PATH={DUMMY_VALUE}\n"
    )
    result, msb_log, _ = invoke_run("bash", cwd=proj, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 1
    assert "reserved" in result.stderr
    assert "PATH" in result.stderr
    assert not any(line.startswith("msb run") for line in msb_log)


def test_secret_conf_is_only_secret_argument_and_argv_preserved(tmp_path):
    """The final invocation carries exactly one --secret-conf naming the
    pair's own secrets.yaml immediately after `run`; the secret name is
    absent from guest -e forwarding and the guest argv after `--` is
    preserved byte-for-byte."""
    home, proj, secret = make_secret_project(tmp_path)
    result, msb_log, _ = invoke_run(
        "tau", "-p", "hello", cwd=proj, home=home, images=(BASE_IMAGE,)
    )
    assert result.returncode == 0, result.stderr
    run_line = _secret_run_line(msb_log)
    assert run_line is not None
    assert run_line.count("--secret-conf") == 1
    assert run_line.startswith(
        f"msb run --secret-conf {secret.resolve()}/secrets.yaml "
    )
    assert DUMMY_VALUE not in run_line
    assert "-e KEY=" not in run_line
    assert run_line.endswith("-- tau -p hello")


def test_secret_values_win_over_env_file_and_reach_runtime_environment(tmp_path):
    """A name assigned in both TAU_ENV_FILE and secrets.env reaches the
    runtime process environment with the secrets.env value, while the
    ordinary -e forwarding of that name is suppressed: the protected
    source, not the ordinary file, owns the variable."""
    home, proj, secret = make_secret_project(tmp_path)
    (home / ".env").write_text("KEY=ordinary-value\nOTHER_KEY=other-value\n")
    env_log = tmp_path / "msb-env.log"
    result, msb_log, _ = invoke_run(
        "bash", cwd=proj, home=home, images=(BASE_IMAGE,),
        env={"MSB_ENV_LOG": str(env_log)},
    )
    assert result.returncode == 0, result.stderr
    run_line = _secret_run_line(msb_log)
    assert run_line is not None
    assert "-e KEY=ordinary-value" not in run_line
    assert "-e OTHER_KEY=other-value" in run_line
    runtime_env = env_log.read_text()
    assert f"KEY={DUMMY_VALUE}" in runtime_env.splitlines()
    assert "KEY=ordinary-value" not in runtime_env.splitlines()


def test_shared_bootstrap_entry_list_drives_snapshot(tmp_path):
    """The snapshot enumeration excludes exactly the documented entries:
    excluded names never become mounts (a symlinked `sessions` entry
    cannot leak the secret directory into the guest), while ordinary
    entries are snapshotted and mounted."""
    home, proj, secret = make_secret_project(tmp_path)
    tau = home / ".tau"
    skills = tau / "skills"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# skill\n")
    (tau / "sessions").symlink_to(secret, target_is_directory=True)
    (tau / "logs").mkdir()
    (tau / "trust.json").write_text("{}\n")
    (tau / "credentials.json").write_text("{}\n")

    result, msb_log, _ = invoke_run("bash", cwd=proj, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 0, result.stderr
    run_line = _secret_run_line(msb_log)
    bootstrap = "/etc/tau-sandbox/bootstrap/tau"
    assert f":{bootstrap}/skills:ro" in run_line
    assert f":{bootstrap}/credentials.json" not in run_line
    assert f":{bootstrap}/sessions" not in run_line
    assert f":{bootstrap}/logs" not in run_line
    assert f":{bootstrap}/trust.json" not in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=0" in run_line


def test_relative_env_file_works_with_present_pair(tmp_path):
    """A caller-supplied relative TAU_ENV_FILE resolves against the launch
    directory, so a present pair forwards it exactly like a no-pair
    launch would."""
    home, proj, secret = make_secret_project(tmp_path)
    (proj / "rel.env").write_text("RELATIVE_KEY=relative-value\n")
    result, msb_log, _ = invoke_run(
        "bash",
        cwd=proj,
        home=home,
        images=(BASE_IMAGE,),
        env={"TAU_ENV_FILE": "rel.env"},
    )
    assert result.returncode == 0, result.stderr
    run_line = _secret_run_line(msb_log)
    assert run_line is not None
    assert "-e RELATIVE_KEY=relative-value" in run_line
    for channel in (result.stdout, result.stderr):
        assert "relative-value" not in channel


def test_bootstrap_snapshot_cleanup_after_runtime_success_and_failure(tmp_path):
    """The bootstrap snapshot is removed after the runtime exits, on
    success and on failure, while the runtime's exit status is
    preserved."""
    home, proj, secret = make_secret_project(tmp_path)
    tau = home / ".tau"
    tau.mkdir()
    (tau / "settings.json").write_text("{}\n")
    result, msb_log, _ = invoke_run("bash", cwd=proj, home=home, images=(BASE_IMAGE,))
    assert result.returncode == 0, result.stderr
    bootstrap = _bootstrap_stage_path(_secret_run_line(msb_log))
    assert not bootstrap.exists()

    result, msb_log, _ = invoke_run(
        "bash", cwd=proj, home=home, images=(BASE_IMAGE,),
        env={"MSB_RUN_STATUS": "3"},
    )
    assert result.returncode == 3
    bootstrap = _bootstrap_stage_path(_secret_run_line(msb_log))
    assert not bootstrap.exists()


def _bootstrap_stage_path(run_line):
    """The bootstrap snapshot directory the runtime invocation mounted."""
    match = re.search(r"-v (/[^ ]*tau-sandbox-bootstrap\.[^/ ]*)/", run_line)
    assert match, run_line
    return pathlib.Path(match.group(1))
