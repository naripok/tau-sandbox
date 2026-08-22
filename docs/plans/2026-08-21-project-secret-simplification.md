# Project-Secret Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 2,279-line defensive project-secret library with a ~120-line pass-through design: source `secrets.env` as trusted shell, hand `secrets.yaml` unmodified to `msb run --secret-conf`.

**Architecture:** A single `project_secrets_prepare` function maps the physical launch directory to `$HOME/.<rel>/`, runs basic sanity checks (pair, types, symlink escape, reserved names), and exports `PROJECT_SECRETS_POLICY_PATH`; `run.sh` sources `secrets.env` after `TAU_ENV_FILE` and inserts one `--secret-conf` argument into the `msb run` argv. All validation beyond that is the runtime's `--secret-conf` contract.

**Tech Stack:** Bash 4.0, `msb` (microsandbox) CLI, pytest harness with fake `msb`/`podman`.

**Standards:** Apply the shared code standards in every task: DRY, low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings, documentation of current state only.

**Feature spec:** `docs/design/2026-08-21-project-secret-simplification-spec.md` (the behavioral contract)

---

## Commands

- Library unit tests: `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py -q`
- run.sh fake-runtime tests: `env -u TAU_LAN_HOSTS uv run pytest tests/test_run.py -q`
- Full suite: `env -u TAU_LAN_HOSTS uv run pytest tests/ -q` — expected: all non-integration tests pass; integration tests skip in this environment (no msb/KVM).
- No linter/formatter/type-checker is configured for the Bash sources; `bash -n <file>` must exit 0 for every edited shell file.

## Task 1: Rewrite the library as pass-through discovery + checks

**Files:**
- Modify: `lib/project-secrets.sh` — full rewrite to ~120 lines
- Test: `tests/test_project_secrets.py` — full rewrite of the fresh-bash harness

**Spec requirement:** ADDED "Paired secret sources"; MODIFIED "Exact secret location mapping".

**Interface:**
- `project_secrets_lexical_path(path, base) -> prints normalized absolute path` — kept as-is from the current library; pure parameter expansion, never touches the filesystem.
- `project_secrets_physical_path(path) -> prints canonical absolute path, nonzero on unresolvable` — kept as-is (cd -P/pwd -P); a dangling entry resolves to its physical parent plus basename.
- `project_secrets_prepare(launch_dir, home, projects_mode, projects_value) -> 0|1` — the only lifecycle call. Side effects on success: sets `PROJECT_SECRETS_PAIR_STATE` to `none` or `present`; for `present`, exports `PROJECT_SECRETS_POLICY_PATH` (physical `secrets.yaml` path), `PROJECT_SECRETS_ENV_PATH` (physical `secrets.env` path, for run.sh to source), and `PROJECT_SECRETS_NAMES` (space-separated declared names). On failure prints exactly one `project-secrets: <message>` line to stderr (message names the offending path or variable, never secret content) and returns 1.
  - `projects_mode` is `default` or `explicit` (any other value fails). Default root = `${home}/Projects`; if it is not a usable directory (`-d`/`-r`/`-x`), discovery disables: `PAIR_STATE=none`, return 0. Explicit mode: empty value or a non-usable root fails with a message naming `TAU_PROJECTS_DIR`. Explicit relative value resolves from the lexical launch directory.
  - Physical launch strictly below physical root → `rel`; derived dir = `${home}/.${rel}` (dot prefixed to the first relative component). Launch equal to/outside the root → `none`.
  - Derived dir with no entry (nor dangling symlink) → `none`. Derived dir that is not `-d`/`-r`/`-x` → fail naming the directory. Derived dir whose physical path lies inside or equals the physical projects root → fail (symlink escape), naming the directory; the equality case (e.g. `.proj` symlinked to `Projects` itself) is deliberately included.
  - Pair: `secrets.env` and `secrets.yaml` — both must be `-f`/`-r` or both absent (`! -e && ! -L`). Exactly one present → fail naming the missing source. Either present but not `-f`/`-r` (directory, device, socket, FIFO, unreadable file, dangling symlink) → fail naming the invalid source. Both absent → `none`.
  - Declared names: one `sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' secrets.env | sort -u` pass. Any name matching the reserved ERE `^(HOME|SHELL|TERM|COLORTERM|USER|LOGNAME|PATH|IFS|PWD|OLDPWD|SHLVL|BASH_ENV|ENV|LD_PRELOAD|LD_LIBRARY_PATH|PYTHONHOME|PYTHONPATH|NODE_OPTIONS)$|^(BASH|TAU_)` → fail naming the offending variable. Failure of the sed/sort pipeline → fail.
  - No other parsing, validation, pinning, freezing, or staging. No state besides the three exported variables above.
- `_project_secrets_fail(message)` — private one-line stderr reporter.

**Behavior:**
- All mapping scenarios from the spec: default/explicit roots, nested exact mapping without inheritance, lexical-out/physical-in eligible, lexical-in/physical-out ineligible, root and outside launches derive nothing, unusable default root disables, invalid explicit root fails.
- All pair-state scenarios: absent directory → none; invalid derived directory fails; absent pair → none; incomplete pair fails identifying the missing source; invalid source types fail; escape fails regardless of pair presence.
- Reserved-name scenarios: exact names and `BASH*`/`TAU_*` prefixes fail before any state is exported; non-assignment lines (comments, blanks, `NAME = value`) declare no names.
- On `present`, four variables are exported: `PROJECT_SECRETS_PAIR_STATE`, `PROJECT_SECRETS_POLICY_PATH` (physical `secrets.yaml`), `PROJECT_SECRETS_ENV_PATH` (physical `secrets.env`), and `PROJECT_SECRETS_NAMES`. On `none`, `PROJECT_SECRETS_PAIR_STATE=none` and the path/name variables are set to EMPTY (never unset, so `set -u` callers are safe).
- The library writes nothing to stdout; diagnostics go to stderr only.
- The entrypoint-namespacing invariant (spec: entrypoint internals stay under `TAU_ENTRYPOINT_`) is already satisfied by the merged `config/entrypoint.sh` and its bounded-dialect guard in tests/test_config.py; this plan does not touch it.

**Tests must prove (fresh-`bash -c` harness sourcing the library, tmp filesystems with symlinks and spaces):**
- Default root mapping, nested exact mapping, nested non-inheritance — one test per scenario
- Explicit absolute and relative root mapping
- Lexical-out/physical-in eligible; lexical-in/physical-out ineligible
- Root launch and outside launch → `none`
- Absent default root → `none`; unusable default root (non-dir, unreadable, dangling) → `none`
- Invalid explicit root (empty, missing, non-dir) → fail naming `TAU_PROJECTS_DIR`
- Invalid `projects_mode` → fail
- Absent derived directory → `none`; invalid derived directory (regular file, dangling symlink, unreadable dir) → fail
- Absent pair → `none`; env-only and yaml-only pairs → fail naming the missing source; invalid source types (dir, FIFO, unreadable file, dangling symlink) → fail naming the invalid source
- Symlink-escape derived dir → fail, both with and without a pair inside
- Present pair exports `PROJECT_SECRETS_POLICY_PATH`, `PROJECT_SECRETS_ENV_PATH`, and declared names from plain and `export` assignments, deduplicated; `none` leaves the path/name variables empty but set
- Reserved exact names (e.g. `PATH`, `IFS`, `BASH_ENV`) and prefixes (`BASH_FOO`, `TAU_X`) → fail naming the variable; comments/blank/spaced-assignment lines declare nothing
- Nothing on stdout; single `project-secrets: ` stderr line on failure

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py -q` — expected: all pass; `bash -n lib/project-secrets.sh` exits 0.

- [ ] Write the failing tests for the behaviors above; run them and confirm each fails for the expected reason (old library interface)
- [ ] Implement the library rewrite
- [ ] Run verification
- [ ] Commit: `git add lib/project-secrets.sh tests/test_project_secrets.py && git commit -m "refactor: pass-through project-secret discovery"` (2-line body per AGENTS.md)

## Task 2: Simplify run.sh integration

**Files:**
- Modify: `run.sh` — replace the discovery/preflight/pinning/freeze block and env/secret plumbing
- Test: `tests/test_run.py` — rewrite every secret-related test and helper (full inventory below); fake `msb` drops its `--version` (`MSB_VERSION`), `MSB_SECRET_TRACE`, and `MSB_REFLECT` modes, and gains an environment dump (`MSB_ENV_LOG`: when set and argv[1] is `run`, append the process environment to that file) so tests can prove which values reach the runtime process environment
- Test: `tests/test_security.py` — rewrite the secret behavior tests (full inventory below). It imports `make_secret_project`, `_secret_run_line`, `BASE_IMAGE`, `DUMMY_VALUE`, `invoke_run`, `FAKE_MSB`, `FAKE_PODMAN` from `test_run.py`, so those exports must be preserved (updated semantics) or both files updated together

test_run.py secret inventory (each is deleted or rewritten): `make_secret_project`, `_secret_run_line`, `test_reset_bypasses_invalid_sources_and_incompatible_runtime` (rewrite: reset works with an invalid pair; no version gate exists to bypass), `test_root_outside_relative_and_nested_mapping_contract` (rewrite for the new marker), `test_exposure_preflight_precedes_images_build_env_snapshot_mounts` (delete: no preflight), `test_incompatible_runtime_precedes_empty_or_malformed_content` (delete: no version gate), `test_present_pair_uses_pinned_msb_for_images_load_rmi_and_run` (delete: no pinning), `test_shared_bootstrap_entry_list_drives_scan_and_snapshot` (rewrite: keep the snapshot-list half, drop the exposure-alias half), `test_generated_secret_conf_is_only_secret_argument` (rewrite: `--secret-conf` value is the pair's physical `secrets.yaml`), `test_cleanup_after_runtime_success_and_failure` (rewrite: only bootstrap-snapshot cleanup remains), `test_no_pair_preserves_existing_invocation_and_old_runtime` (keep behavior, drop the stale version-gate docstring), `test_relative_env_file_works_with_present_pair` (keep behavior, drop the POSIX-mode docstring).

test_security.py secret inventory: `test_env_alias_and_trusted_instrumentation_contract` (delete: alias vetting removed), `test_no_launcher_channel_contains_dummy_value` (rewrite: with a present pair, the fake msb's `MSB_ENV_LOG` proves the dummy value reaches the runtime process environment while argv, msb/podman logs, launcher stdout/stderr, and image inputs contain it nowhere), `test_secret_hosts_do_not_add_network_rules` (keep unchanged: it is the coverage for "Secret policy adds no private egress rule").

**Spec requirement:** ADDED "Paired secret sources" (sourcing order, reserved-name fail-fast, suppression wiring); MODIFIED "Protected secret boundary" (single `--secret-conf` placement), "Environment forwarding" (suppression), "Reset bypasses secret discovery".

**Interface / behavior (exact edit contract):**
- Delete the entire present-pair lifecycle block (`for entry ... project_secrets_validate_metadata || exit 1`, currently lines ~177–203) including the `MSB_BIN="$PROJECT_SECRETS_MSB_PATH"` override; `MSB_BIN="msb"` stays and is used everywhere.
- Delete the `BOOTSTRAP_ENTRIES` capture; the snapshot loop iterates `"$RAW_CONFIG_DIR"/*` with `dotglob nullglob` inline with the EXACT current skip list (`credentials.json|sessions|logs|trust.json|trust.json.lock|trust.json.pending|.host-config-bootstrapped|.host-config-synced`) — do not broaden the patterns.
- Delete `project_secrets_validate_env_source` call and its comment; delete `project_secrets_cleanup` from the `cleanup()` trap (bootstrap-snapshot `rm -rf` remains the only cleanup).
- Delete only the POSIX-mode sourcing comment at lines 36–39; the relative-`TAU_ENV_FILE` absolutization `case` at lines 40–43 stays (its rationale comment is rewritten: slash-less relative names must resolve from the launch directory, not PATH) and lines 44–51 (CPUS/MEM/PIDS, TAU_LAN_HOSTS, SCRIPT_DIR) are untouched.
- Restructure the environment-forwarding section: the `if [ -f "$ENV_FILE" ]` guard wraps ONLY the `TAU_ENV_FILE` sourcing; then, UNCONDITIONALLY (outside that guard), compute `PROJECTS_MODE`/`PROJECTS_VALUE` (moved down from the old block), call `project_secrets_prepare "$(pwd)" "$HOME" "$PROJECTS_MODE" "$PROJECTS_VALUE" || exit 1`, and on `present` run `set -a; source "$PROJECT_SECRETS_ENV_PATH"; set +a` (path exported by the library, Task 1); then the name-extraction `while` loop and `ENV_ARGS` construction stay guarded by `if [ -f "$ENV_FILE" ]` as today, with the suppression `case` inside it (safe under `set -u` because `PROJECT_SECRETS_NAMES` is always set — empty on `none`).
- Env-forwarding loop: replace `project_secrets_is_guest_name`/`note_ordinary_guest_name` with a `case " $PROJECT_SECRETS_NAMES " in *" $key "*) continue ;; esac` suppression.
- Final exec: replace the `project_secrets_exec_runtime` branch with `"$MSB_BIN" run --secret-conf "$PROJECT_SECRETS_POLICY_PATH" "${RUN_ARGS[@]}"` when `PAIR_STATE=present`; the else branch stays `"$MSB_BIN" run "${RUN_ARGS[@]}"` (RUN_ARGS never contains `run` itself). Guest argv after `--` is untouched.
- `--reset` handling, image build, packages, mounts, and net rules are unchanged.
- run.sh must remain `set -euo pipefail`, Bash 4.0, and produce no new stdout.

**Tests must prove (fake msb logs argv and, via `MSB_ENV_LOG`, its process environment; no KVM):**
- Present pair → fake-msb log shows exactly one `run --secret-conf <policy>` immediately after `run`, and the policy path is the pair's physical `secrets.yaml`
- No pair → no `--secret-conf` in argv; `--reset` performs only `msb volume rm` (no discovery, no sourcing) even with an invalid pair present
- Mapping contract through the launcher: default root, nested non-inheritance, explicit absolute/relative `TAU_PROJECTS_DIR`, outside launch, absent default root, invalid explicit root fails with a `TAU_PROJECTS_DIR` message
- Name declared in both env file and `secrets.env` → no `-e KEY=` argument for it; ordinary names still forwarded
- Reserved name in `secrets.env` → launch fails before any `msb run` call, stderr names the variable
- Incomplete pair and invalid derived directory → launch fails naming the source/directory
- Guest argv after `--` preserved byte-for-byte with a present pair
- Secret values win: a name assigned in both `TAU_ENV_FILE` and `secrets.env` reaches the fake runtime's process environment (`MSB_ENV_LOG`) with the `secrets.env` value
- Launcher non-disclosure: the dummy secret value appears in no launcher stdout/stderr line, no msb/podman argv or log, and no image-build input (test_security.py rewrite)
- Secret destinations add no `--net-rule` (kept `test_secret_hosts_do_not_add_network_rules`)

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_run.py -q` — expected: all pass; `bash -n run.sh` exits 0.

- [ ] Write the failing tests for the behaviors above; run them and confirm each fails for the expected reason
- [ ] Implement the run.sh edits
- [ ] Run verification
- [ ] Commit: `git add run.sh tests/test_run.py && git commit -m "refactor: simplify run.sh secret integration"` (2-line body)

## Task 3: Align conftest and integration tests

**Files:**
- Modify: `tests/conftest.py` — delete `compatible_msb_secrets`, `MSB_VERSION_OUTPUT_RE`, `MSB_COMPATIBLE_MIN/MAX`, `_missing_compatible_runtime_prerequisite`, `MISSING_COMPATIBLE_RUNTIME_PREREQUISITE`, `skip_without_compatible_msb_secrets`; extract the KVM/platform checks (Linux: readable+writable /dev/kvm; macOS: arm64; else unsupported) into a new `skip_without_virtualization` skipif marker; update `ProjectSecretFixture` default `PROJECT_SECRET_YAML` to the runtime-native grammar (`TEST_PROJECT_API_KEY:\n  value: "${TEST_PROJECT_API_KEY}"\n  allow:\n    - api.example.com\n`)
- Modify: `tests/test_integration.py` — rewrite `TestProjectSecretRuntimeBoundary` (see below), gate it on `skip_without_msb` + `skip_without_podman` + the new `skip_without_virtualization`; drop the `skip_without_compatible_msb_secrets` import

**Spec requirement:** MODIFIED "Protected secret boundary" (placeholder observability), REMOVED "Compatible secret runtime", REMOVED "Collision-free source references".

**Interface / behavior:**
- `TestProjectSecretRuntimeBoundary` keeps only tests meaningful under pass-through:
  - `test_project_secret_is_guest_placeholder_only` — unchanged (guest `printenv` shows `$MSB_<NAME>`, dummy value absent from stdout/stderr).
  - `test_reserved_names_reject_before_boot` — keep the launcher-side rejection proof (e.g. `PATH`), asserting the launch fails naming the variable and no guest runs.
  - `test_external_secret_fixture_and_volumes_are_cleaned` — unchanged.
- Delete: synthetic-source absence test (no synthetic names exist), literal `${OTHER}` source-text test (depends on launcher grammar), and all `TAU_SANDBOX_SECRET_SOURCE_*` machinery from the fake msb in test_run.py if any remains after Task 2.
- `ProjectSecretFixture` keeps its shape; only the default YAML text changes (fixture tests pass `yaml_text` explicitly where needed).

**Tests must prove:**
- The file collects without the deleted conftest symbols; skipped in this environment, correct skip reasons
- `env -u TAU_LAN_HOSTS uv run pytest tests/test_integration.py --collect-only -q` shows `TestProjectSecretRuntimeBoundary` contributing exactly the three kept tests

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/ -q` — expected: full suite passes with integration tests skipped.

- [ ] Update conftest and integration tests
- [ ] Run verification
- [ ] Commit: `git add tests/conftest.py tests/test_integration.py && git commit -m "test: align integration boundary with pass-through secrets"` (2-line body)

## Task 4: Documentation sync

**Files:**
- Modify: `docs/SPEC.md` — apply the delta: replace the 12 secret-domain requirements with the spec's 1 ADDED + 5 MODIFIED texts, delete the 7 REMOVED ones
- Modify: `README.md` — rewrite the "Protected project secrets" section and every secret mention (mapping, pair, sourced env, pass-through policy, reserved names, suppression, placeholders, prerequisites without the version range, security table, testing section)
- Modify: `config/APPEND_SYSTEM.md` — shrink the secret boundary section to placeholders vs. ordinary values under the pass-through contract
- Test: `tests/test_config.py` and `tests/test_security.py` — update doc-contract assertions that reference removed sections/terms (e.g. strict grammars, version range, staging); keep the entrypoint bounded-dialect guard untouched

**Spec requirement:** MODIFIED "Project secret documentation" (both scenarios).

**Behavior:**
- README documents: `TAU_PROJECTS_DIR` (default `${HOME}/Projects`, unusable default disables, invalid explicit fails), exact mapping with no inheritance, sourced `secrets.env` (shell syntax, trusted), runtime-native `secrets.yaml` passed to `--secret-conf` with a minimal working example using `value: "${NAME}"`, the exact reserved set, forwarding suppression, placeholders and destination-scoped substitution as the runtime's contract, reset behavior, network independence, and the prerequisite of a runtime supporting `--secret-conf` (no version range).
- `APPEND_SYSTEM.md` states: protected secrets reach the guest only as `$MSB_<NAME>` placeholders substituted per the host policy; `env` never reveals real values; ordinary `-e` forwarding is raw plaintext.
- `docs/SPEC.md` keeps RFC 2119 style and all non-secret requirements byte-identical.

**Tests must prove:**
- Doc-contract tests assert the new README/APPEND_SYSTEM content markers and the SPEC.md structure (new requirement names present, removed ones absent)
- Full suite green

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/ -q` — expected: all pass (integration skipped).

- [ ] Update the three docs and the doc-contract tests
- [ ] Run verification
- [ ] Commit: `git add docs/SPEC.md README.md config/APPEND_SYSTEM.md tests/test_config.py tests/test_security.py && git commit -m "docs: document pass-through project secrets"` (2-line body)
