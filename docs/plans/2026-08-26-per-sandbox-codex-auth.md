# Per-Sandbox Codex OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each sandbox its own OpenAI Codex OAuth session so concurrent sandboxes never fight over one rotating refresh token.

**Architecture:** Stop mounting the host credential file into the guest; each sandbox uses a project-local credential in its persistent volume. A self-contained host helper runs the Codex browser flow on the host and writes the credential into the project volume through the microsandbox SDK. The Tau fork gains a cross-process refresh lock so two sandboxes on one project volume spend a rotating token at most once.

**Tech Stack:** Bash (`run.sh`), Python 3.12+ (host helper, guest wrapper), the `naripok/tau` fork (`provider_runtime.py`), microsandbox Python SDK (`VolumeFs.write`).

**Standards:** Apply the shared code standards in every task: DRY, minimal implementation (YAGNI), low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings, documentation of current state only, writing-developer-facing-text prose.

**Feature spec:** `docs/design/2026-08-26-per-sandbox-codex-auth-spec.md` (the behavioral contract)

---

## Commands

This work spans two repositories. Run each task in the repository named in its **Repo** line.

**Sandbox repo** (this project, worktree `/workspace/.worktrees/per-sandbox-codex-auth`):

- Test suite: `/workspace/.venv/bin/python -m pytest -q`
- Test one file: `/workspace/.venv/bin/python -m pytest tests/test_run.py -q`
- Note: three tests fail on a dirty baseline (test env leaks `TAU_LAN_HOSTS`; a README contract test). They are pre-existing and unrelated. Compare against the baseline, not zero failures.

**Fork repo** (`naripok/tau`, local clone):

- Setup: `git clone https://github.com/naripok/tau <fork-dir> && cd <fork-dir>`
- Test suite: `uv run pytest`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format --check .`
- Type check: `uv run mypy`
- Test one file: `uv run pytest tests/test_provider_runtime.py`

---

## Phase A — Fork (`naripok/tau`)

These tasks land in the fork. The sandbox repo pins the resulting commit as `TAU_REF` in Task 5.

### Task 1: Cross-process refresh lock in the fork

**Repo:** fork (`naripok/tau`)

**Files:**
- Modify: `src/tau_coding/provider_runtime.py` — serialize refresh across processes
- Test: `tests/test_provider_runtime.py`

**Spec requirement:** Concurrent refresh spends a rotating token once

**Interface:**
- New module-private context manager `_file_refresh_lock(store_path: Path) -> Iterator[None]` — acquires an exclusive advisory lock on a sibling lock file for the duration of a refresh. Opens `<store_path>.lock` (creating it if absent), takes an exclusive `flock` (POSIX) or `msvcrt.locking` (Windows), yields, then releases and closes. Follows the existing `_lock`/`_unlock` pattern in `src/tau_coding/project_trust.py` (lines ~640-665), including the `os.name == "nt"` branch. On a platform that provides `flock`/`msvcrt`, an `OSError` from open or lock propagates as a refresh failure (hard error); only a platform lacking both primitives skips the file lock.
- `_refresh_lock(credential_name: str) -> asyncio.Lock` — unchanged (keeps the in-process per-loop lock).
- Both refresh call sites gain the file lock **inside** the existing `asyncio.Lock` and around the re-read + refresh + write:
  - `OpenAICodexCredentialResolver._refresh_if_needed` (lines ~254-268): after `async with _refresh_lock(...)`, wrap the `stored = ... get_oauth ... refresh ... set_oauth` block in `with _file_refresh_lock(self._credential_store.path):`.
  - `OAuthRuntimeCredentialResolver.__call__` (lines ~325-337): same wrapping around its re-read + refresh + `set_oauth`.

**Behavior:**
- The file lock is held across the re-read of the store and the network refresh, so a second process that acquires the lock re-reads the rotated credential and skips its own refresh.
- The lock file lives next to the credential file (`<credentials.json>.lock`), so two sandboxes on the same project volume contend on the same lock, while two sandboxes on different volumes use different locks. The lock file is a persistent sibling; it is never deleted (deletion would reopen a race), so do not add cleanup logic.
- The in-process `asyncio.Lock` remains the outer guard; the file lock is the cross-process guard nested inside it. Order is always asyncio-lock-then-file-lock to avoid lock-ordering issues.
- The lock is the mechanism that enforces the spec's "spend a rotating token at most once." Therefore a **lock-acquisition failure on a supported platform is a hard error**: if opening or locking the sibling lock file raises `OSError` on a platform that has `flock`/`msvcrt`, refresh must fail with a clear error rather than proceed unlocked (proceeding unlocked risks the `refresh_token_reused` the spec forbids). The only tolerated absence is a platform with no `flock`/`msvcrt` at all (exotic non-POSIX, non-Windows), where the code falls back to the in-process lock only; that path is out of scope for the supported Linux guest and host.

**Tests must prove:**
- Two OS processes sharing one credential file do not both spend the refresh token. The two refreshes MUST run in separate processes (subprocesses or `ProcessPoolExecutor`), not two coroutines on one loop — otherwise the existing in-process `asyncio.Lock` serializes them and the test passes with or without the file lock. Pre-seed a store with an expired credential, monkeypatch the network refresh in both children to record calls and return a rotated token, start both refreshes concurrently, and assert the network refresh ran exactly once across both processes and both callers got the rotated credential. (A mock authorization server that counts token requests is an acceptable alternative.)
- The lock file path is `<store_path>.lock` beside the credential file.
- A lock-acquisition `OSError` on a supported platform surfaces as a refresh error (no silent unlocked refresh).
- The existing `test_refresh_locks_are_not_shared_between_event_loops` behavior is preserved (in-process lock still per-loop).

**Check:** `uv run pytest tests/test_provider_runtime.py && uv run ruff check src/tau_coding/provider_runtime.py && uv run mypy` — expected: all pass

- [ ] Write the failing tests for the behaviors above. Run them and check that each fails for the expected reason
- [ ] Implement the interface and behavior
- [ ] Run verification (tests, lint, type check)
- [ ] Commit: `git add src/tau_coding/provider_runtime.py tests/test_provider_runtime.py && git commit -m "feat: cross-process refresh lock for shared OAuth credentials"`

### Task 2: Record the fork commit for pinning

**Repo:** fork (`naripok/tau`)

**Files:**
- None (records a value used by Task 5)

**Spec requirement:** Concurrent refresh spends a rotating token once (enabler)

**Interface:**
- Run `git -C <fork-dir> rev-parse HEAD` after Task 1 and record the full commit hash.

**Behavior:**
- Produce the exact `TAU_REF` value the sandbox repo pins. No code change. The implementer carries the hash directly into Task 5; nothing is written to the repo in this task.

**Tests must prove:**
- N/A (value capture). The commit must contain Task 1's change: `git -C <fork-dir> log --oneline -1` shows the refresh-lock commit.

**Check:** `git -C <fork-dir> rev-parse HEAD` — expected: prints the Task 1 commit hash

- [ ] Capture and record the commit hash for use in Task 5

---

## Phase B — Sandbox repo (this project)

### Task 3: Stop sharing the credential file in `run.sh`

**Repo:** sandbox (worktree)

**Files:**
- Modify: `run.sh` — remove the credential mount and always run with project-local credentials
- Test: `tests/test_run.py`

**Spec requirement:** Project-local credential storage; Writable shared credentials exception (MODIFIED)

**Interface:**
- In the mounts section, delete the conditional block that mounts `"$CONFIG_DIR/credentials.json:/etc/tau-sandbox/shared/credentials.json"` and sets `TAU_SANDBOX_SHARED_CREDENTIALS=1`/`0`. Replace with an unconditional `ENV_ARGS+=(-e "TAU_SANDBOX_SHARED_CREDENTIALS=0")`.
- Remove the now-unused `CONFIG_DIR/credentials.json` existence check. Verify `credentials.json` remains in the existing bootstrap-exclusion lists in `run.sh` and `config/entrypoint.sh`; no change is expected there — do not add a duplicate entry.

**Behavior:**
- No launch mounts the host credential file, regardless of whether it exists on the host.
- The guest always sees `TAU_SANDBOX_SHARED_CREDENTIALS=0`.
- The entrypoint's existing `elif` branch (which removes the legacy shared-credential symlink when the flag is off) becomes the live path; on next boot each sandbox drops the symlink and uses a project-local file.

**Tests must prove:**
- The generated `msb run` command contains no mount for `credentials.json` and contains `TAU_SANDBOX_SHARED_CREDENTIALS=0`, when host credentials exist.
- Same when host credentials are absent.
- The bootstrap snapshot excludes `credentials.json` (no read-only mount of it under `/etc/tau-sandbox/bootstrap/tau/`).

**Check:** `/workspace/.venv/bin/python -m pytest tests/test_run.py -q` — expected: pass

- [ ] Write the failing tests for the behaviors above. Run them and check that each fails for the expected reason
- [ ] Implement the interface and behavior
- [ ] Run verification (tests)
- [ ] Commit: `git add run.sh tests/test_run.py && git commit -m "feat: stop sharing host credentials; use project-local credential files"`

### Task 4: Remove the in-place credential writer from `tau-wrapper.py`

**Repo:** sandbox (worktree)

**Files:**
- Modify: `config/tau-wrapper.py` — drop the `_save` monkeypatch, keep prompt injection
- Test: `tests/test_config.py`

**Spec requirement:** Writable shared credentials exception (MODIFIED)

**Interface:**
- Delete `_ORIGINAL_SAVE`, `_save_credentials`, the `credentials.FileCredentialStore._save = _save_credentials` assignment, and the `from tau_coding import credentials` import. Remove the now-unused `json`/`os` imports if nothing else uses them.
- Keep the `sys.argv` `--append-system-prompt` injection and `app()` entry exactly as-is.
- The file's module docstring changes from "supports credential file mounts" to describing only the invariant-prompt injection.

**Behavior:**
- The wrapper no longer patches `FileCredentialStore`. Tau uses its stock atomic writer for the project-local credential file. The whole-file-update guarantee in the MODIFIED spec scenario (a concurrent reader observes the old or the new complete credential, never a partial file) rests entirely on the stock `FileCredentialStore._save`, which writes a temp file and atomically renames it over the target.
- The wrapper still injects `--append-system-prompt /etc/tau-sandbox/APPEND_SYSTEM.md` on every invocation.

**Tests must prove:**
- The wrapper source contains no `_save` patch and no `credentials` import.
- The wrapper still inserts `--append-system-prompt` into `sys.argv` before calling `app()`.
- The whole-file guarantee holds under the stock writer. This is a **characterization/guard test**, not a failing-first test: the stock `FileCredentialStore._save` already writes a temp file and atomically renames it over the target, so this test passes before and after the wrapper change and exists to guard against regression. Assert that a concurrent reader of the credential file always observes complete, parseable JSON (never a partial file) while the stock writer saves.

**Check:** `/workspace/.venv/bin/python -m pytest tests/test_config.py -q` — expected: pass

- [ ] Write the failing tests for the behaviors above. Run them and check that each fails for the expected reason
- [ ] Implement the interface and behavior
- [ ] Run verification (tests)
- [ ] Commit: `git add config/tau-wrapper.py tests/test_config.py && git commit -m "refactor: drop in-place credential writer; keep prompt injection only"`

### Task 5: Pin the new fork commit in the Containerfile

**Repo:** sandbox (worktree)

**Files:**
- Modify: `Containerfile` — bump `TAU_REF`

**Spec requirement:** Concurrent refresh spends a rotating token once (enabler: ships the fork lock to guests)

**Interface:**
- Set `ARG TAU_REF=` to the commit hash recorded in Task 2.
- Update the comment above `ARG TAU_REF` only if it references the pinned version; keep it describing the current state.

**Behavior:**
- The next image build installs the fork commit that carries the cross-process refresh lock.

**Tests must prove:**
- `Containerfile` `TAU_REF` equals the recorded hash (a test that reads the file and asserts the arg value matches the expected commit, or asserts it differs from the old pinned commit `a8a2b47110834cfbb09f5bf8340ca67b48d64416`).

**Check:** `grep '^ARG TAU_REF' Containerfile` — expected: prints the new hash

- [ ] Update `ARG TAU_REF` to the recorded commit
- [ ] Commit: `git add Containerfile && git commit -m "chore: bump naripok/tau ref for cross-process refresh lock"`

### Task 6: Host login helper `tau-login-openai`

**Repo:** sandbox (worktree)

**Files:**
- Create: `lib/tau-login-openai` — self-contained host CLI (Python 3, stdlib + vendored flow)
- Modify: `install.sh` — link the helper into `~/.local/bin`
- Test: `tests/test_login_helper.py`

**Spec requirement:** Host login helper produces a readable project credential

**Interface:**
- `tau-login-openai PROJECT_PATH` — one positional arg, the project directory. Exit 0 on success, non-zero with a stderr message on failure.
- Volume-name derivation: `tau-persist-<sanitized-basename>-<sha256(realpath)[:8]>`, reusing the exact `sanitize_project_name` and hashing rule from `run.sh` (duplicate the small function; keep it byte-identical to `run.sh`).
- Vendored OAuth flow (stdlib `urllib`, `http.server`, `hashlib`, `secrets`, `webbrowser`), ported from the fork's `src/tau_coding/oauth.py`: PKCE S256 pair (`create_pkce_pair`), state, the fixed callback `http://localhost:1455/auth/callback`, the authorization URL with the same client id/scope/params (`create_openai_codex_authorization_flow`, ~line 139), code exchange against `https://auth.openai.com/oauth/token` (`exchange_openai_codex_authorization_code`, ~line 256), and account-id extraction from the access-token JWT (`account_id_from_access_token`, ~line 309). Returns an `(access, refresh, expires_ms, account_id)` tuple.
- Credential write: build the `{"openai-codex": {"type":"oauth","access","refresh","expires","account_id"}}` JSON (indent 2, sorted keys, trailing newline — matching `FileCredentialStore._save` in the fork's `src/tau_coding/credentials.py`). Write the flow's `expires_ms` value verbatim as `expires`. Write the file to `/home/tau/.tau/credentials.json` inside the project volume via the microsandbox Python SDK `VolumeFs.write`. If the named volume does not exist, create it through the SDK first.
- Headless fallback: if `webbrowser.open` is unavailable or the callback server cannot bind port 1455, print the authorization URL and read a pasted redirect URL from stdin; parse and validate `state` before exchange (port `parse_authorization_input`, ~line 172 of `src/tau_coding/oauth.py`).

**Behavior:**
- On a host with a browser, the flow completes with no paste: browser opens, user approves, redirect reaches the host server on 1455, script exchanges and writes the credential.
- On a headless host or occupied port, the script prints the URL and accepts a pasted redirect URL.
- The written file is byte-compatible with what guest Tau reads (same format, same path in the volume).
- No guest port is published and no guest network access is required; the write goes through the host msb SDK.
- The script never prints tokens; progress and errors go to stdout/stderr without secrets.

**Tests must prove:**
- Volume-name derivation matches `run.sh` for a set of project paths (including a name needing sanitization).
- Two distinct project paths yield distinct volume names and credential paths (project isolation).
- The produced credential JSON parses and has the exact keys/types guest Tau expects (`type`, `access`, `refresh`, `expires` int, `account_id`).
- The paste path validates `state` and rejects a mismatched state.
- The write step calls the SDK with the project volume and the `/home/tau/.tau/credentials.json` path (mock the SDK).
- No token value appears in stdout/stderr.

**Check:** `/workspace/.venv/bin/python -m pytest tests/test_login_helper.py -q` — expected: pass

- [ ] Write the failing tests for the behaviors above. Run them and check that each fails for the expected reason
- [ ] Implement the interface and behavior
- [ ] Run verification (tests)
- [ ] Commit: `git add lib/tau-login-openai install.sh tests/test_login_helper.py && git commit -m "feat: host login helper writes per-project Codex credentials"`

### Task 7: Documentation and living-spec sync

**Repo:** sandbox (worktree)

**Files:**
- Modify: `README.md` — replace the shared-credentials description with the per-project model and the helper usage
- Modify: `docs/SPEC.md` — apply the spec delta (replace `Writable shared credentials exception`, add the new Project Credentials requirements)
- Modify: `config/APPEND_SYSTEM.md` — update the credential description to project-local
- Modify: `tests/test_config.py` — update the README/SPEC contract assertions to the per-project model
- Test: `tests/test_config.py`

**Spec requirement:** all (documentation of current state)

**Interface:**
- `README.md`: in the credentials/mount section, describe that each sandbox uses a project-local credential in its volume, that the host credential file is never mounted, and that `tau-login-openai PROJECT_PATH` logs a project in from the host. Update the security/threat-model lines that reference sharing `credentials.json` (e.g. the "Only `credentials.json` is shared" row) to state credentials are project-local.
- `docs/SPEC.md`: replace the `Writable shared credentials exception` requirement text and scenarios with the MODIFIED version from the feature spec, and add the three ADDED requirements (`Project-local credential storage`, `Host login helper produces a readable project credential`, `Concurrent refresh spends a rotating token once`) with their scenarios.
- `config/APPEND_SYSTEM.md`: state that credentials are project-local in the per-project home and that login uses the host helper. Keep it in sync with `run.sh` and the Containerfile per `AGENTS.md`.

**Behavior:**
- Docs describe only the current per-project behavior, with no reference to the removed shared-credential mechanism.

**Tests must prove:**
- Existing README/SPEC contract tests pass against the updated docs.
- No doc references the removed in-place writer or the shared `credentials.json` mount as current behavior.

**Check:** `/workspace/.venv/bin/python -m pytest tests/test_config.py -q` — expected: pass

- [ ] Update `README.md`, `docs/SPEC.md`, `config/APPEND_SYSTEM.md`
- [ ] Run verification (tests)
- [ ] Commit: `git add README.md docs/SPEC.md config/APPEND_SYSTEM.md tests/test_config.py && git commit -m "docs: per-project credential model and host login helper"`

---

## Execution notes

- **Order:** Phase A (fork) before Phase B Task 5, because Task 5 pins the fork commit from Task 2. Tasks 3, 4, 6, 7 do not depend on the fork and can run after Task 1 in parallel with Task 2/5 sequencing.
- **Two repos:** fork commits land in `naripok/tau`; sandbox commits land in the `per-sandbox-codex-auth` worktree branch. Do not commit sandbox changes to `main`.
- **Integration check (manual, after all tasks):** build the image, run two sandboxes on the same project, log in via `tau-login-openai`, and confirm concurrent Codex calls do not return `refresh_token_reused`. This needs a real OpenAI login and is not part of the automated suite.
