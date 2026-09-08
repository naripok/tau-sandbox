# opencode Fork Implementation Plan

> **For agentic workers:** Execute this plan task-by-task. Each task is one
> commit and ends with the unit suite as green as its baseline (one pre-existing
> failure: `test_containerfile_pins_tau_ref_with_refresh_lock`, replaced by the
> opencode pin test in Task 1).

**Goal:** Fork the tau-sandbox launcher project so it runs the opencode agent
(huggingface tau is replaced end to end) with the same isolation, persistence,
config-sync, secret, and login-helper guarantees.

**Architecture:** Keep the launcher, runtime, and test architecture unchanged.
Swap the guest agent binary and every agent-specific surface: image install,
guest paths, config sync targets, credential document, wrapper mechanism, and
naming. The agent identity changes from `tau` to `opencode`; launcher
configuration moves from the `TAU_` prefix to `OPENCODE_SANDBOX_` because
opencode owns the `OPENCODE_` prefix for its own runtime variables.

**Tech Stack:** Bash, microsandbox (`msb`), podman, Python 3 (login helper),
pytest, opencode v1.18.29 release binaries.

**Standards:** DRY, minimal implementation, low cyclomatic complexity, no
unnecessary fallbacks, informative docstrings, documentation of current state
only, writing-developer-facing-text prose.

**Feature spec:** `docs/SPEC.md` (updated in Task 4 to describe the fork).

---

## Research findings (verified against opencode v1.18.29)

Checked out `github.com/sst/opencode` at tag `v1.18.29` and ran the release
binary. Facts the fork relies on:

1. **Install.** Release assets include `opencode-linux-x64.tar.gz` and
   `opencode-linux-arm64.tar.gz` (glibc; musl variants exist). Each tarball
   holds one `opencode` binary at the archive root. The install script
   (`https://opencode.ai/install`) accepts `--version <v>`; direct tarball
   download pins the version without script behavior risk.
2. **Global paths** (xdg-basedir, `packages/core/src/global.ts`):
   data `~/.local/share/opencode`, config `~/.config/opencode`,
   state `~/.local/state/opencode`, cache `~/.cache/opencode`,
   log `data/log`, repos `data/repos`. Auth lives at `data/auth.json`.
   Session storage root is `data/storage` (`src/storage/storage.ts`).
3. **Config resolution** (`src/config/config.ts`): global
   `config.json`, then `opencode.json`, then `opencode.jsonc` under the
   config dir; then the file named by env var `OPENCODE_CONFIG` merged on
   top; then project `opencode.json(c)` files walking up from the working
   directory. The merge helper concatenates `instructions` arrays with
   dedup, so no later source can shadow an earlier `instructions` entry.
   Verified empirically: `opencode debug config` shows global, sandbox, and
   project instructions concatenated in that order.
4. **Instructions.** Each `config.instructions` entry names a file, glob, or
   URL. Absolute paths glob inside their dirname. Content is injected into
   the system prompt as "Instructions from: <path>". This is the injection
   point for the immutable sandbox context document.
5. **Auth document** (`src/auth/index.ts`): `data/auth.json`, mode 0600,
   a JSON object keyed by provider id. OAuth entries carry
   `{type: "oauth", refresh, access, expires, accountId?}` with `expires`
   in epoch milliseconds; API entries carry `{type: "api", key}`. Env var
   `OPENCODE_AUTH_CONTENT` overrides the file (not used by this fork).
6. **OpenAI/ChatGPT OAuth** (`src/plugin/openai/codex.ts`): client id
   `app_EMoamEEZ73f0CkXaXp7hrann`, issuer `https://auth.openai.com`,
   redirect `http://localhost:1455/auth/callback`, scope
   `openid profile email offline_access`, provider id `openai`.
   Identical constants to the vendored tau helper, so the host login
   helper ports with format changes only. Account id extraction reads the
   `id_token` claims first, then the access token claims. Upstream refresh
   has no cross-process lock: per-project `auth.json` isolation stays the
   fork's race mitigation for different projects; two sandboxes of one
   project follow stock opencode refresh behavior.
7. **Update check** (`src/cli/upgrade.ts`): skipped when config
   `autoupdate` is `false` or env `OPENCODE_DISABLE_AUTOUPDATE` is truthy
   (`"true"`/`"1"`).
8. **CLI.** `opencode` starts the TUI; `opencode run [message..]` runs
   non-interactive; `opencode providers` (alias `auth`) manages
   credentials; `opencode debug config` dumps resolved config.
9. **Config dir writes.** opencode seeds a default `opencode.json` into the
   config dir and installs plugin dependencies there (`package.json`,
   `node_modules`, lockfiles, `.gitignore`). Host-sync excludes those
   generated artifacts so every start does not copy a host `node_modules`
   tree into the snapshot.

## Naming map

| tau-sandbox | opencode-sandbox fork |
| --- | --- |
| `tau-sandbox` (alias, stage prefix) | `opencode-sandbox` |
| `TAU_IMAGE` | `OPENCODE_SANDBOX_IMAGE` |
| `TAU_CONFIG_DIR` | `OPENCODE_SANDBOX_CONFIG_DIR` |
| `TAU_AGENTS_DIR` (`.agents` mount) | removed; opencode reads global config from the synced config dir |
| `TAU_ENV_FILE` | `OPENCODE_SANDBOX_ENV_FILE` |
| `TAU_CPUS` / `TAU_MEM` / `TAU_PIDS` | `OPENCODE_SANDBOX_CPUS` / `OPENCODE_SANDBOX_MEM` / `OPENCODE_SANDBOX_PIDS` |
| `TAU_LAN_HOSTS` | `OPENCODE_SANDBOX_LAN_HOSTS` |
| `TAU_PROJECTS_DIR` | `OPENCODE_SANDBOX_PROJECTS_DIR` |
| `TAU_NO_UPDATE_CHECK` | `OPENCODE_DISABLE_AUTOUPDATE` (opencode's own variable) |
| image `tau-agent-isolated` | `opencode-agent-isolated` |
| volumes `tau-persist-` / `tau-sessions-` / `tau-logs-` | `opencode-persist-` / `opencode-sessions-` / `opencode-logs-` |
| `.tau-packages` | `.opencode-packages` |
| `/etc/tau-sandbox`, `/var/lib/tau-sandbox` | `/etc/opencode-sandbox`, `/var/lib/opencode-sandbox` |
| guest user `tau`, home `/home/tau` | guest user `opencode`, home `/home/opencode` |
| entrypoint scratch `TAU_ENTRYPOINT_` | `OPENCODE_SANDBOX_ENTRYPOINT_` |
| host config `~/.tau`, project `.tau` discovery | `${XDG_CONFIG_HOME:-~/.config}/opencode`, project `.opencode` discovery |
| guest config copy `~/.tau/*` | `~/.config/opencode/*` |
| sessions link `~/.tau/sessions` | `~/.local/share/opencode/storage` |
| logs link `~/.tau/logs` | `~/.local/share/opencode/log` |
| credential `~/.tau/credentials.json` | `~/.local/share/opencode/auth.json` |
| trust.json exclusions | removed; opencode has no trust file |
| wrapper `config/tau-wrapper.py` (`--append-system-prompt`) | `config/opencode-wrapper.sh` exporting `OPENCODE_CONFIG=/etc/opencode-sandbox/opencode.json` |
| pinned `TAU_REF` pip install of naripok/tau | pinned `OPENCODE_VERSION` release tarball |
| helper `lib/tau-login-openai` | `lib/opencode-login-openai` (provider `openai`, `auth.json`, `expires` ms, `accountId`) |
| reserved secret prefix `TAU_` | `OPENCODE_SANDBOX_` |

## Tasks

### Task 1: Guest image and sandbox config files

**Files:**
- Modify: `Containerfile` — install the pinned opencode release tarball for
  the build architecture into `/opt/opencode/bin`; drop the tau venv; copy
  `config/opencode.json` to `/etc/opencode-sandbox/opencode.json`; install
  `config/opencode-wrapper.sh` as `/usr/local/bin/opencode`; user
  `opencode` (uid 1000, home `/home/opencode`); directory names
  `/etc/opencode-sandbox`, `/var/lib/opencode-sandbox`.
- Create: `config/opencode.json` — sandbox config merged via
  `OPENCODE_CONFIG`: `{"$schema": "https://opencode.ai/config.json",
  "autoupdate": false, "instructions": ["/etc/opencode-sandbox/APPEND_SYSTEM.md"]}`.
- Create: `config/opencode-wrapper.sh` — `set -euo pipefail` bash wrapper;
  exports `OPENCODE_CONFIG` and `OPENCODE_DISABLE_AUTOUPDATE=true`; execs
  `/opt/opencode/bin/opencode` with caller arguments preserved.
- Delete: `config/tau-wrapper.py`.
- Modify: `config/entrypoint.sh` — paths per the naming map; create
  `~/.local/share/opencode` before linking storage/log volumes; sync host
  config into `~/.config/opencode` with exclusions `.host-config-synced`,
  `.host-config-bootstrapped`, `node_modules`, `package.json`,
  `package-lock.json`, `bun.lock`, `.gitignore`, `auth.json`; drop tau
  legacy recovery blocks (root-owned `.tau` move, credentials symlink
  cleanup); export `OPENCODE_DISABLE_AUTOUPDATE=true`.
- Modify: `config/.bashrc` — prompt `[\\u@opencode-sandbox \\W]`, header.
- Modify: `config/APPEND_SYSTEM.md` — opencode environment reference: same
  storage/security/secrets structure, opencode paths, `.opencode-packages`,
  `OPENCODE_SANDBOX_*` variables, `Agent: opencode`, injection described as
  config instructions via the wrapper.
- Modify: `tests/test_containerfile.py`, `tests/test_config.py` —
  containerfile contract, entrypoint contracts (reserved prefix
  `OPENCODE_SANDBOX_ENTRYPOINT_`, manifest loop, directives), bashrc,
  APPEND_SYSTEM doc contracts, wrapper tests (stub binary records argv and
  env), drop the `tau_coding` characterization test and tau wrapper tests.

**Check:** `env -u TAU_LAN_HOSTS .venv/bin/python -m pytest tests/test_containerfile.py tests/test_config.py tests/test_run.py tests/test_security.py tests/test_project_secrets.py tests/test_makefile.py -q` — only the pre-existing pin failure remains, then zero after this task replaces it. `bash -n` on new shell files.

### Task 2: Launcher, installer, Makefile, secrets library

**Files:**
- Modify: `run.sh` — naming map applied; config discovery probes `.opencode`;
  default config dir `${XDG_CONFIG_HOME:-${HOME}/.config}/opencode`; remove
  the `.agents` mount and `TAU_AGENTS_DIR`; bootstrap snapshot and mount
  targets under `/etc/opencode-sandbox/bootstrap/opencode`; exclusions per
  Task 1; mount `config/opencode.json` read-only next to `APPEND_SYSTEM.md`;
  volume-name length cap 228 (18-char longest prefix + 8-char hash + margin).
- Modify: `Makefile` — image name, `opencode` target.
- Modify: `lib/project-secrets.sh` — reserved regex prefix
  `OPENCODE_SANDBOX_`, error text `OPENCODE_SANDBOX_PROJECTS_DIR`.
- Modify: `tests/test_run.py`, `tests/test_security.py`,
  `tests/test_project_secrets.py`, `tests/test_makefile.py`,
  `tests/conftest.py`, `tests/test_integration.py` — identifiers per the
  naming map; discovery tests use `.opencode`; remove `.agents` coverage if
  present; integration expectations use the new mount paths.

**Check:** full unit suite as in Task 1; `bash -n run.sh lib/project-secrets.sh`.

### Task 3: Host login helper

**Files:**
- Rename: `lib/tau-login-openai` → `lib/opencode-login-openai` — writes
  `<volume>/.local/share/opencode/auth.json` (mode 0600); provider id
  `openai`; oauth entry `{type, access, refresh, expires, accountId}` with
  `expires` in epoch milliseconds; account id extracted from `id_token`
  claims first, then access token claims; originator `opencode`; seeding
  copies only `{"type": "api", "key": string}` entries from host
  `~/.local/share/opencode/auth.json`, never oauth or wellknown entries;
  volume prefix `opencode-persist-`; docstring states the vendored flow
  comes from opencode's `plugin/openai/codex.ts`.
- Modify: `install.sh` — image name, helper link
  `opencode-login-openai`, alias `opencode-sandbox`, usage text.
- Modify: `tests/test_login_helper.py` — format, path, seeding, and volume
  expectations; keep browser/paste flow coverage.

**Check:** full unit suite; `python3 -m py_compile`-equivalent via the test run.

### Task 4: Documentation and package metadata

**Files:**
- Modify: `README.md` — fork README: opencode quick start, config sync,
  `.opencode-packages`, protected secrets with `OPENCODE_SANDBOX_` names,
  per-project `auth.json` and `opencode-login-openai`, reset, security,
  development.
- Modify: `docs/SPEC.md` — requirements under the naming map; drop the
  `.agents` requirement and the tau-fork concurrent-refresh guarantee;
  document stock opencode in-place `auth.json` writes as agent behavior the
  launcher does not change; wrapper requirement describes the
  `OPENCODE_CONFIG` instructions injection with the baked fallback copy.
- Modify: `pyproject.toml` — name `opencode-sandbox`, description.
- Modify: `uv.lock` — regenerate with `uv lock`.
- Modify: `tests/test_config.py` — README and SPEC doc-contract tests for
  the fork text.

**Check:** full unit suite green.

### Task 5: Verification and review

- Run the full unit suite with `TAU_*` variables unset; expect 0 failures.
- `bash -n` every changed shell file.
- Run the real opencode v1.18.29 binary with an isolated HOME and
  `OPENCODE_CONFIG` pointing at a copy of the fork's sandbox config;
  check `opencode debug config` resolves `autoupdate: false` and the
  instructions entry.
- Dispatch a code-review subagent over the branch diff; fix endorsed
  findings; commit fixes.
