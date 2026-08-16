# Base-Update Propagation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make base-image updates (Tau `TAU_REF` bumps, `config/` changes) invalidate per-project `.tau-packages` images so they rebuild through the existing approval gate, and prune superseded images from the microsandbox cache.

**Architecture:** `run.sh` derives a `BASE_HASH` (first 8 hex of the SHA-256 of the concatenated hex SHA-256 digests of `Containerfile` + every regular file under `config/`, bytewise path order) and names package images `tau-agent-isolated-<project>-<BASE_HASH>-<PKG_HASH>`. When the base changes the tag is absent from `msb images -q`, so the existing interactive approval path builds a new image (podman's layer cache busts the `RUN pip install tau@$TAU_REF` layer). After loading, `run.sh` prunes cached images of the current package content at older base hashes (legacy single-hash tags included) but never images with other package hashes. A missing `Containerfile` or `config/` aborts package launches loudly; other launches are unaffected.

**Tech Stack:** Bash (`run.sh`), pytest with fake `msb`/`podman` binaries (black-box tests).

**Standards:** Apply the shared code standards in every task: DRY, low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings, documentation of current state only. All test functions need docstrings stating what they prove and why.

**Feature spec:** `docs/design/2026-08-16-base-update-propagation-spec.md`

**Delta spec:** `docs/design/2026-08-16-base-update-propagation-delta.md`

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `run.sh` | Launcher: image resolution, build, prune | Add `compute_base_hash`, `prune_superseded_package_images`; extend tag naming; update header comment |
| `tests/test_run.py` | Black-box tests with fake `msb`/`podman` | Refactor invokers; new harness helpers; new + updated tests |
| `docs/SPEC.md` | Living spec | Splice modified package-declarations requirement + add pruning requirement |
| `README.md` | User docs | `.tau-packages` section: naming rule, base-update flow, pruning |
| `Containerfile` | Image definition | Upgrade-vehicle comment |
| `config/APPEND_SYSTEM.md` | Agent-facing environment reference | Base-update approval sentence |

**Known environment quirk (read before running tests):** this sandbox forwards `TAU_LAN_HOSTS` from the host env file, and the tests copy `os.environ`, so two pre-existing tests fail with unexpected `--net-rule` args. Run the suite as `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests -q` (or plain `pytest` on a host without the variable). Do NOT modify those two tests — they are correct and unrelated.

---

### Task 1: Test harness upgrades

**Files:**
- Modify: `tests/test_run.py` (imports, `FAKE_MSB`, `invoke_run`, `invoke_run_tty`, new helpers)

**Delta requirement:** Infrastructure for every test task (no behavioral change itself).

- [ ] **Step 1: Add `shutil` import and the `rmi` case to `FAKE_MSB`**

At the top of `tests/test_run.py`, add `import shutil` after `import pathlib`.

In `FAKE_MSB`, add an `rmi` case (before the `*) exit 0` fallback) so run.sh's prune is logged and can be made to fail:

```python
    rmi) [ "${MSB_RMI_FAIL:-0}" = "1" ] && exit 1 || exit 0 ;;
```

- [ ] **Step 2: Add `script` and `images` parameters to the invokers**

`invoke_run` currently looks like:

```python
def invoke_run(*args, env=None, cwd=None, images=()):
    """Run run.sh non-interactively (no TTY) with fake msb/podman.

    Returns (result, msb_log_lines, podman_log_lines).
    """
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tau-run-test-"))
    fake_bin, msb_log, podman_log, images_file = _fake_bin(tmp)
    images_file.write_text("\n".join(images) + ("\n" if images else ""))
    fake_env = _fake_env(tmp, cwd, fake_bin, msb_log, podman_log, images_file, env)

    result = subprocess.run(
        [str(REPO_ROOT / "run.sh"), *args],
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
```

Replace its signature and the subprocess call with a `script` parameter, keeping everything else identical:

```python
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
```

`invoke_run_tty` currently looks like:

```python
def invoke_run_tty(cwd, env=None, answer="y\n"):
    """Run run.sh in a pseudo-terminal and answer the package approval
    prompt. Returns (returncode, all_output).

    Needed because run.sh only builds per-project package images after an
    explicit interactive approval.
    """
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tau-run-tty-"))
    fake_bin, msb_log, podman_log, images_file = _fake_bin(tmp)
    fake_env = _fake_env(tmp, cwd, fake_bin, msb_log, podman_log, images_file, env)
    fake_env["TERM"] = "xterm"

    master, slave = pty.openpty()
    # The pty/select/timeout loop below stays untouched.
    proc = subprocess.Popen(
        [str(REPO_ROOT / "run.sh"), "bash"],
        ...
```

Replace its signature with `def invoke_run_tty(cwd, env=None, answer="y\n", images=(), script=REPO_ROOT / "run.sh"):`, add `images_file.write_text("\n".join(images) + ("\n" if images else ""))` right after `_fake_bin(tmp)`, and change the `Popen` list to `[str(script), "bash"]`. Everything else stays identical.

- [ ] **Step 3: Add the `_base_hash` and `_stub_repo` helpers**

Add after `_fake_env`:

```python
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
```

```python
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
```

- [ ] **Step 4: Run the existing suite to confirm the harness refactor is neutral**

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests/test_run.py -q`
Expected: `17 passed` (current 17 test_run tests, no failures). If the two known `TAU_LAN_HOSTS` failures appear, `env -u TAU_LAN_HOSTS` was not applied.

- [ ] **Step 5: Commit**

```bash
git add tests/test_run.py
git commit -m "test: harness support for stub repos, rmi failure, base hashes"
```

---

### Task 2: Base-hash naming and missing-input aborts

**Files:**
- Modify: `tests/test_run.py`
- Modify: `run.sh` (resolution-block comment ~lines 96-100, `compute_hash` block, image resolution block ~lines 120-126)

**Delta requirement:** MODIFIED "Per-project package declarations" (base-hash naming, non-empty definition, abort rule; scenarios: base input change invalidates, added/removed input, non-file entries, unchanged reuse, missing-input aborts, other launches unaffected).

- [ ] **Step 1: Update the existing package-build test to the two-hash name**

In `test_per_project_image_with_packages_builds_and_loads`, replace:

```python
    expected_image = f"tau-agent-isolated-{tmp_path.name}-{pkg_hash}"
```

with:

```python
    expected_image = f"tau-agent-isolated-{tmp_path.name}-{_base_hash()}-{pkg_hash}"
```

- [ ] **Step 2: Write the new failing tests**

Add these tests to `tests/test_run.py`:

```python
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
```

```python
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
```

```python
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
```

```python
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
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests/test_run.py -q -k "current_package or missing_containerfile or missing_config or added_base_input or per_project" -v`
Expected: `test_current_package_image_skips_build_and_prune` FAILS (old naming does not match the current tag → non-interactive refuse, rc 1); `test_missing_containerfile_aborts_package_launch` FAILS (no abort yet → builds and exits 0); `test_missing_config_aborts_package_launch` FAILS (same); `test_added_base_input_changes_tag` FAILS (both runs derive the same old-style tag); `test_per_project_image_with_packages_builds_and_loads` FAILS (expected name now contains the base hash).

- [ ] **Step 4: Implement `compute_base_hash` and the new tag name in run.sh**

In `run.sh`, immediately after the `compute_hash` function, add:

```bash
compute_base_hash() {
    # Freshness key for the base the package image is baked from: the
    # Containerfile and every regular file under config/ (dotfiles included),
    # digests concatenated in bytewise path order. Directories and other
    # non-regular entries are skipped. A missing Containerfile or config/
    # directory aborts loudly: a package launch must never derive a tag whose
    # freshness cannot be verified against the current inputs.
    [ -f "$SCRIPT_DIR/Containerfile" ] || {
        echo "Error: $SCRIPT_DIR/Containerfile is missing; cannot derive a package image tag." >&2
        exit 1
    }
    [ -d "$SCRIPT_DIR/config" ] || {
        echo "Error: $SCRIPT_DIR/config is missing; cannot derive a package image tag." >&2
        exit 1
    }
    {
        sha256sum "$SCRIPT_DIR/Containerfile"
        find "$SCRIPT_DIR/config" -maxdepth 1 -type f -print0 |
            LC_ALL=C sort -z | xargs -0 -r sha256sum
    } | awk '{ printf "%s", $1 }' | sha256sum | cut -c1-8
}
```

In the image-resolution block, replace:

```bash
    if [ "$HAS_PACKAGES" -eq 1 ]; then
        PKG_HASH=$(compute_hash ".tau-packages")
        IMAGE_NAME="tau-agent-isolated-${PROJECT_NAME}-${PKG_HASH}"
    fi
```

with:

```bash
    if [ "$HAS_PACKAGES" -eq 1 ]; then
        PKG_HASH=$(compute_hash ".tau-packages")
        BASE_HASH=$(compute_base_hash)
        IMAGE_NAME="tau-agent-isolated-${PROJECT_NAME}-${BASE_HASH}-${PKG_HASH}"
    fi
```

Also update the section comment directly above the resolution block. It currently reads:

```bash
# Image reference resolution.
# TAU_IMAGE overrides everything: the reference is passed to msb verbatim
# and image management (build/load) is skipped entirely — the user manages
# that image externally (e.g. `make build`). Otherwise, use per-project
# naming when .tau-packages lists packages, else the shared base image.
```

Replace the last sentence with:

```bash
# that image externally (e.g. `make build`). Otherwise, use per-project
# naming when .tau-packages lists packages, else the shared base image.
# Per-project names embed a hash of the base inputs (Containerfile and
# config/), so base updates invalidate the tag and trigger the approval-
# gated rebuild on the next run.
```

Note: `compute_base_hash` calls `exit 1` inside a `$(...)` command substitution; the failing substitution makes `BASE_HASH=$(compute_base_hash)` exit nonzero, and `set -e` terminates the script after the error message prints. This is the intended abort path.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests/test_run.py -q -k "current_package or missing_containerfile or missing_config or added_base_input or per_project" -v`
Expected: all PASS. The repo's own `config/__pycache__/` directory is present in the working tree, so `test_per_project_image_with_packages_builds_and_loads` passing also proves non-regular entries are skipped by the hash pipeline (they would break a naive `config/*` glob).

- [ ] **Step 6: Add the two unaffected-path guard tests**

```python
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
```

```python
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
```

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests/test_run.py -q -k "ignores_missing" -v`
Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
git add run.sh tests/test_run.py
git commit -m "feat: embed base-input hash in per-project image tags

Package images are now tau-agent-isolated-<project>-<base>-<pkg>; base
changes invalidate the tag and trigger the approval-gated rebuild.
Missing Containerfile or config aborts package launches; shared-base
and TAU_IMAGE paths are unaffected."
```

---

### Task 3: Prune superseded package images

**Files:**
- Modify: `tests/test_run.py`
- Modify: `run.sh` (after the `podman save "$IMAGE_NAME" | msb load` line)

**Delta requirement:** ADDED "Superseded package images are pruned" (scenarios: base-triggered rebuild removes superseded, legacy single-hash pruned, legacy other-content kept, same-basename kept, earlier content kept, fresh build succeeds, failed removal tolerated).

- [ ] **Step 1: Write the failing tests**

```python
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
```

```python
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
```

```python
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests/test_run.py -q -k "stale_package or prune_rmi" -v`
Expected: `test_stale_package_image_rebuilds_and_prunes_superseded` FAILS (no prune exists: no rmi lines; the run line also boots the legacy name). The other two pass already as guards — `test_stale_package_image_refuses_non_interactively` pins the gate across stale states, `test_prune_rmi_failure_does_not_fail_launch` pins the tolerance contract that the prune implementation must keep.

- [ ] **Step 3: Implement the prune function and call it**

In `run.sh`, immediately after `compute_base_hash`, add:

```bash
prune_superseded_package_images() {
    # Cache hygiene after a package-image build: remove the images this build
    # supersedes — the legacy single-hash tag of the current package content
    # and any older base version of it. Images tagged with any other package
    # hash (same-basename projects, earlier .tau-packages contents) are never
    # removed. Failed removals are tolerated: pruning never blocks the launch.
    local hex8='[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    local cached_ref
    while IFS= read -r cached_ref; do
        case "$cached_ref" in
            localhost/tau-agent-isolated-${PROJECT_NAME}-${PKG_HASH}:latest | \
            localhost/tau-agent-isolated-${PROJECT_NAME}-${hex8}-${PKG_HASH}:latest)
                [ "$cached_ref" = "$IMAGE_REF" ] && continue
                msb rmi "$cached_ref" >/dev/null 2>&1 || true
                ;;
        esac
    done < <(msb images -q)
}
```

Note the case patterns must stay unquoted so the `$hex8` character class and the hash variables are re-interpreted as patterns; `PROJECT_NAME` and `PKG_HASH` contain only `[a-zA-Z0-9._-]`, so no glob metacharacters enter the pattern.

The `[ "$cached_ref" = "$IMAGE_REF" ] && continue` guard (spec: "SHALL NOT remove the image it just loaded") is not directly exercised by these tests — the fake msb's image listing is static and never shows the freshly loaded tag. It is defense-in-depth for real caches, where the fresh tag legitimately matches the two-hash pattern; keep it.

In the build block, after the load line:

```bash
    podman save "$IMAGE_NAME" | msb load
```

add, before the closing `fi` of the `if [ "${SKIP_IMAGE_CHECK:-0}" != "1" ] && ! msb images -q | grep -qx "$IMAGE_REF"` block:

```bash
    if [ "$HAS_PACKAGES" -eq 1 ]; then
        prune_superseded_package_images
    fi
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests/test_run.py -q -k "stale_package or prune_rmi" -v`
Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
git add run.sh tests/test_run.py
git commit -m "feat: prune superseded package images on rebuild

After loading a rebuilt per-project image, remove cached legacy and
older-base tags of the current package content. Other package hashes
(same-basename projects, earlier contents) are kept; rmi failures are
silently tolerated."
```

---

### Task 4: Documentation sync

**Files:**
- Modify: `docs/SPEC.md` (living spec)
- Modify: `README.md`
- Modify: `Containerfile`
- Modify: `config/APPEND_SYSTEM.md`

**Delta requirement:** All MODIFIED/ADDED requirements must be reflected in the living spec; user and agent docs must describe the new behavior only (documentation of current state only). `tests/test_containerfile.py` and `tests/test_config.py` assert only substring presence (tool list, TAU_REF, launchers, config substrings), so the additions below keep them green — run the full suite in Task 5 to confirm.

- [ ] **Step 1: Splice the modified requirement into `docs/SPEC.md`**

In the section `### Requirement: Per-project package declarations`, replace the two paragraphs:

```markdown
A non-empty `.tau-packages` file SHALL select image `tau-agent-isolated-<basename>-<package-hash>`, where the hash is derived from the file's raw bytes. The launcher SHALL require interactive approval before building a missing package-specific image.
```

with:

```markdown
For this requirement, a `.tau-packages` file is non-empty when it declares at least one package name after stripping comments, blank lines, and surrounding whitespace. A non-empty `.tau-packages` file SHALL select image `tau-agent-isolated-<basename>-<base-hash>-<package-hash>`, where:

- `<package-hash>` is derived from the raw bytes of `.tau-packages`;
- `<base-hash>` is the first eight hexadecimal characters of the SHA-256 of the text formed by concatenating, in lexicographic path order, the hex-encoded SHA-256 digests (64 lowercase hex characters, no separators) of the raw bytes of each regular file directly under `config/` (including dotfiles), preceded by the digest of the repository `Containerfile`. It SHALL change when the content, or set, of those files changes and SHALL be stable when none does. Non-regular entries under `config/` SHALL be ignored.

The launcher SHALL require interactive approval before building a missing package-specific image. When the launcher would otherwise derive a package image tag (a non-empty `.tau-packages` file is present and no `TAU_IMAGE` override is set), a missing repository `Containerfile` or `config/` directory SHALL abort the launch with an error rather than derive a tag whose freshness cannot be verified against the current inputs.
```

Keep the `The file format SHALL:` bullet list and the `Comment-only and empty files SHALL use the shared base image.` sentence exactly as they are. Keep the existing `#### Scenario: Non-interactive rebuild is refused` and append these scenarios after it (before the next `### Requirement:` heading):

```markdown
#### Scenario: Base input change invalidates the package image

- GIVEN a project whose package image was built from an earlier build context
- WHEN the content of `Containerfile` or a `config/` file changes and the `.tau-packages` content does not
- THEN the launcher SHALL select a different image tag than the previously built one
- AND building it SHALL require the same interactive approval as any missing package image

#### Scenario: Added base input changes the tag

- GIVEN a package image was tagged from a base-input set without a particular regular file under `config/`
- WHEN that file is added to `config/` without changing any other input
- THEN the launcher SHALL select a different image tag than the previously built one

#### Scenario: Removed base input changes the tag

- GIVEN a package image was tagged from a base-input set that included a particular regular file under `config/`
- WHEN that file is removed without changing any other input
- THEN the launcher SHALL select a different image tag than the previously built one

#### Scenario: Non-file config entries do not affect the hash

- GIVEN `config/` contains a directory alongside its regular files and the package image tag exists in the cache
- WHEN the project launches
- THEN the launcher SHALL select the same tag as it would without the directory
- AND it SHALL boot the cached image without building

#### Scenario: Unchanged inputs reuse the cached image

- GIVEN the package image tag derived from the current build context exists in the microsandbox cache
- WHEN the project launches and stdin is not a terminal
- THEN the launcher SHALL NOT build anything
- AND the launcher SHALL NOT remove any image from the cache
- AND the launcher SHALL boot the cached image

#### Scenario: Non-interactive base-triggered rebuild is refused

- GIVEN the package image tag is missing because the build context changed and stdin is not a terminal
- WHEN the project launches
- THEN startup SHALL fail without building

#### Scenario: Missing base inputs abort a package-tag launch

- GIVEN the project has a non-empty `.tau-packages` file and no `TAU_IMAGE` override, and the repository `Containerfile` or the `config/` directory is missing
- WHEN the project launches
- THEN the launcher SHALL abort with an error and SHALL NOT build or boot an image

#### Scenario: Missing base inputs do not affect other launches

- GIVEN the repository `Containerfile` or the `config/` directory is missing
- WHEN a project without a non-empty `.tau-packages` file, or with a `TAU_IMAGE` override, launches
- THEN the launcher SHALL proceed with the shared base image or the override and SHALL NOT abort
```

- [ ] **Step 2: Add the pruning requirement to `docs/SPEC.md`**

Insert a new section directly after the package-declarations section (i.e., right before `### Requirement: Environment forwarding`):

```markdown
### Requirement: Superseded package images are pruned

Package image tags are keyed by basename, base hash, and package hash, so same-basename projects share one tag namespace: projects with identical base inputs and identical `.tau-packages` content use the same tag, while different package contents produce different tags under the same basename prefix.

When the launcher builds a package-specific image, it SHALL remove from the microsandbox cache every other image whose reference is `localhost/tau-agent-isolated-<basename>-<package-hash>:latest` (legacy single-hash form of the current package content) or `localhost/tau-agent-isolated-<basename>-<8 hex>-<package-hash>:latest` (any base version of the current package content). It SHALL NOT remove the image it just loaded. Inherent to the shared tag namespace, an image carrying the current package hash at another base hash is removed whether this project or a same-basename project with identical `.tau-packages` content produced it. Images tagged with any other package hash — including those of same-basename projects with different `.tau-packages` content and those of earlier package contents of this project — SHALL NOT be removed; in particular, a legacy single-hash tag whose hash differs from the current package hash SHALL NOT be removed. A failed removal SHALL NOT fail the build, the load, or the launch, and SHALL NOT be reported as an error.

#### Scenario: Base-triggered rebuild removes the superseded image

- GIVEN the cache contains a package image for the current package content whose tag derives from an older base hash (two-hash form)
- WHEN the launcher rebuilds the package image for the changed base
- THEN the old image SHALL be removed from the cache
- AND the newly built image SHALL remain

#### Scenario: Legacy single-hash image is pruned

- GIVEN the cache contains a package image tagged in the legacy single-hash form for the same project and the same package content
- WHEN the launcher rebuilds the package image
- THEN the legacy image SHALL be removed from the cache
- AND the newly built image SHALL remain

#### Scenario: Legacy tag of another content is kept

- GIVEN the cache contains a package image tagged in the legacy single-hash form whose hash differs from the current package hash
- WHEN the launcher rebuilds the package image
- THEN that image SHALL remain in the cache

#### Scenario: Same-basename projects keep their package images

- GIVEN two projects with the same basename and different `.tau-packages` contents both have cached package images
- WHEN the launcher rebuilds the package image for one of them
- THEN the other project's image SHALL remain in the cache

#### Scenario: Earlier package content image survives a rebuild

- GIVEN the cache contains a package image tagged from earlier `.tau-packages` content of the same project
- WHEN the launcher rebuilds the package image for the current content
- THEN the earlier-content image SHALL remain in the cache

#### Scenario: Fresh build with an empty cache succeeds

- GIVEN the cache contains no package images for the project
- WHEN the launcher builds the package image for the first time
- THEN the build and load SHALL succeed and no removal error SHALL be reported

#### Scenario: Failed removal does not fail the launch

- GIVEN the cache contains a superseded package image whose removal from the cache fails
- WHEN the launcher rebuilds the package image for the changed base
- THEN the build, the load, and the launch SHALL succeed
- AND no removal error SHALL be reported
```

- [ ] **Step 3: Update `README.md`**

In `## Per-Project System Dependencies`, replace step 4:

```markdown
4. The image name includes a hash of `.tau-packages`, so changes trigger a new approval.
```

with:

```markdown
4. The image name includes hashes of `.tau-packages` and of the sandbox base inputs (`Containerfile` and `config/`), so package or base changes (e.g. Tau upgrades) trigger a new approval and rebuild.
```

In the same section, replace the Security paragraph:

```markdown
**Security:** Every change to `.tau-packages` requires explicit user approval before the image is rebuilt. The agent can write `.tau-packages` but cannot bypass the approval gate. Non-interactive mode (pipes, CI) refuses to rebuild without approval.
```

with:

```markdown
**Security:** Every change to `.tau-packages` requires explicit user approval before the image is rebuilt. The agent can write `.tau-packages` but cannot bypass the approval gate. Non-interactive mode (pipes, CI) refuses to rebuild without approval. The same gate covers base updates: per-project images embed a hash of the base inputs, so rebuilding the sandbox base invalidates them and the next interactive run asks for approval again. Rebuilds prune superseded images of the current package content from the microsandbox cache; images of other package contents (same-basename projects, earlier `.tau-packages` contents) are kept.
```

- [ ] **Step 4: Update the `Containerfile` comment**

Replace:

```markdown
# Tau pinned to a commit of the naripok/tau fork (currently the 0.3.10
# release). The sandbox image is the upgrade vehicle:
# rebuild the image (make build) to update Tau or system packages.
```

with:

```markdown
# Tau pinned to a commit of the naripok/tau fork (currently the 0.3.10
# release). The sandbox image is the upgrade vehicle:
# rebuild the image (make build) to update Tau or system packages.
# Per-project package images embed a hash of this file and config/;
# changing either invalidates them and triggers an approval-gated rebuild
# on the project's next run.
```

- [ ] **Step 5: Update `config/APPEND_SYSTEM.md`**

Replace:

```markdown
The next run requires user approval before building the package-specific image.
```

with:

```markdown
The next run requires user approval before building the package-specific image. The same approval applies when the sandbox base changes (e.g. a Tau upgrade): the per-project image tag embeds the base inputs, so base updates invalidate it and the next interactive start rebuilds with approval.
```

- [ ] **Step 6: Verify the splice is complete and consistent**

Run: `rg -n "base-hash|Superseded package images|Non-interactive" docs/SPEC.md | head -20`

Expected: the base-hash bullet, the prune requirement heading, the retained `Non-interactive rebuild is refused`, and the new `Non-interactive base-triggered rebuild is refused` all present.

Run: `rg -n "Per-project|base inputs|hash" README.md | head -10`
Expected: the updated step 4 and Security paragraph visible.

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests -q`
Expected: `67 passed, 20 skipped` — the two pre-existing `TAU_LAN_HOSTS` environment failures must NOT appear with `env -u TAU_LAN_HOSTS`.

- [ ] **Step 7: Commit**

```bash
git add docs/SPEC.md README.md Containerfile config/APPEND_SYSTEM.md
git commit -m "docs: base-hash naming, approval flow, and pruning in spec and docs

Living spec gains the modified package-image naming and the pruning
requirement; README, Containerfile, and APPEND_SYSTEM.md describe the
base-update rebuild flow."
```

---

### Task 5: Final verification and review handoff

**Files:** none (verification only)

**Delta requirement:** All requirements pass their tests; the change is ready for review.

- [ ] **Step 1: Run the full suite**

Run: `env -u TAU_LAN_HOSTS ./.venv/bin/python -m pytest tests -q`
Expected: `67 passed, 20 skipped`, zero failures. If the two `TAU_LAN_HOSTS` tests fail, `env -u TAU_LAN_HOSTS` was dropped and the failures are environmental (host env file forwards the variable into the sandbox), not caused by this change.

- [ ] **Step 2: Run shellcheck on run.sh**

Run: `shellcheck run.sh` (if installed) or `bash -n run.sh` and `bash -n config/entrypoint.sh`
Expected: no syntax errors. If shellcheck is unavailable, `bash -n` is sufficient.

- [ ] **Step 3: Show the final diff summary for review**

Run: `git log --oneline 8a80afa..HEAD && git diff 8a80afa..HEAD --stat`
Expected: the feature commits from Tasks 1-4 plus the design artifacts (proposal/spec/delta commits), with the implementation touching only `run.sh`, `tests/test_run.py`, and the four doc files.

- [ ] **Step 4: Hand off for code review (no commit)**

Report completion with the diff summary; a review agent then inspects the working tree. Fix any review findings, re-run Step 1, and commit the fixes. Final commit message style: `fix: <what the review found>`.
