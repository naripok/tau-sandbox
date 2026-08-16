"""Unit tests for run.sh using fake msb/podman binaries.

These tests prove run.sh generates the correct microsandbox invocation
(mounts, flags, env forwarding, image resolution, package approval flow)
without requiring KVM or a built image.
"""
import hashlib
import os
import pathlib
import pty
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


def _fake_env(tmp: pathlib.Path, cwd, fake_bin, msb_log, podman_log, images_file, env):
    fake_env = os.environ.copy()
    fake_env["PATH"] = f"{fake_bin}:{fake_env['PATH']}"
    # HOME follows the project dir so tests write ~/.env|~/.tau next to cwd.
    fake_env["HOME"] = str(cwd or tmp)
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
    if containerfile:
        shutil.copy(REPO_ROOT / "Containerfile", repo / "Containerfile")
    if config:
        shutil.copytree(REPO_ROOT / "config", repo / "config")
    return repo

FAKE_MSB = """#!/bin/bash
echo "msb $*" >> "$MSB_LOG"
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


def invoke_run(*args, env=None, cwd=None, images=(), script=REPO_ROOT / "run.sh"):
    """Run run.sh non-interactively (no TTY) with fake msb/podman.

    script selects which run.sh copy to execute; tests use a stub repo
    copy to exercise base-input handling without touching the real repo.
    Returns (result, msb_log_lines, podman_log_lines).
    """
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tau-run-test-"))
    fake_bin, msb_log, podman_log, images_file = _fake_bin(tmp)
    images_file.write_text("\n".join(images) + ("\n" if images else ""))
    fake_env = _fake_env(tmp, cwd, fake_bin, msb_log, podman_log, images_file, env)

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


def invoke_run_tty(cwd, env=None, answer="y\n", images=(), script=REPO_ROOT / "run.sh"):
    """Run run.sh in a pseudo-terminal and answer the package approval
    prompt. Returns (returncode, all_output).

    Needed because run.sh only builds per-project package images after an
    explicit interactive approval.
    """
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tau-run-tty-"))
    fake_bin, msb_log, podman_log, images_file = _fake_bin(tmp)
    images_file.write_text("\n".join(images) + ("\n" if images else ""))
    fake_env = _fake_env(tmp, cwd, fake_bin, msb_log, podman_log, images_file, env)
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


def test_run_script_mounts_host_config_as_bootstrap_with_shared_credentials(tmp_path):
    """Host config is a read-only bootstrap source; credentials stay shared."""
    (tmp_path / ".env").write_text("")
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    (tau_dir / "skills").mkdir()
    (tau_dir / "settings.json").write_text("{}\n")
    (tau_dir / "credentials.json").write_text("{}\n")
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
    shared_credentials = "/etc/tau-sandbox/shared/credentials.json"
    assert f"-v {tau_dir.resolve()}/credentials.json:{shared_credentials}" in run_line
    assert f"{tau_dir.resolve()}/credentials.json:{shared_credentials}:ro" not in run_line
    assert f"{tau_dir.resolve()}/credentials.json:/home/tau/.tau/credentials.json" not in run_line
    assert f"-v {tau_dir.resolve()}/sessions" not in run_line
    assert f"-v {tau_dir.resolve()}/logs" not in run_line
    assert f"-v {tau_dir.resolve()}/trust.json" not in run_line
    assert f"-v {tau_dir.resolve()}/trust.json.lock" not in run_line
    assert f"-v {tmp_path.resolve()}/.agents:/home/tau/.agents:ro" in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=1" in run_line


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
    """Absent host config leaves Tau state local to the persistent home."""
    (tmp_path / ".env").write_text("")
    result, msb_log, _ = invoke_run("bash", cwd=tmp_path)
    run_line = next(line for line in msb_log if line.startswith("msb run"))
    assert f"{tmp_path.resolve()}/.tau" not in run_line
    assert f"{tmp_path.resolve()}/.agents" not in run_line
    assert "-e TAU_SANDBOX_SHARED_CREDENTIALS=0" in run_line
    assert ":/var/lib/tau-sandbox/sessions" in run_line
    assert ":/var/lib/tau-sandbox/logs" in run_line


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
