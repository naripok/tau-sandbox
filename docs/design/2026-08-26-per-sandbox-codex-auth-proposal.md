# Proposal: Per-Sandbox Codex OAuth

## Intent

Every sandbox shares the host `credentials.json`. OpenAI Codex OAuth rotates the
refresh token on each use: the old token dies at once and only the refreshing
process receives the successor. When two sandboxes share the file, both read the
same expired token and both refresh. One wins; the other receives
`refresh_token_reused` and cannot recover, because the successor token never
reaches it. OpenAI can also revoke the whole token family on reuse.

The file mount adds a second failure. Microsandbox bind-mounts the host file by
inode. Host Tau saves with an atomic rename, which creates a new inode. The
sandbox keeps the old inode and never sees the new file. No lock repairs this:
only the refresher learns the new token, and sandboxes refresh on independent
schedules.

The fix is to stop sharing the credential. Each project gets an independent
OAuth session and its own token family, stored in the project's persistent
volume. A host-side login helper performs the browser flow on the host (where a
browser exists) and writes the credential into the project volume through the
microsandbox API. No port is published and the isolation boundary stays closed.

## Scope

**In scope:**

- Stop mounting host `credentials.json` into the guest; sandboxes always use a
  project-local credential file in the persistent home volume.
- A host-side `tau-login-openai` helper that runs the Codex browser flow on the
  host and writes the resulting credential into a named project's persistent
  volume through the microsandbox SDK (`VolumeFs.write`), self-contained by
  vendoring the OAuth flow (no host dependency on the fork package).
- A cross-process refresh lock in the Tau fork so concurrent refreshes of one
  stored credential (two sandboxes on the same project volume) spend a rotating
  refresh token at most once. Same-project concurrency is a supported case.
- Remove the in-place credential writer from the guest wrapper; stock atomic
  rename works on the project-local file.
- Documentation and spec updates for the per-project credential model.

**Out of scope:**

- Publishing port 1455 or any inbound access into the sandbox.
- A true RFC 8628 device flow. The OpenAI discovery document lists no device
  endpoint and allows only `authorization_code` and `refresh_token` grants, so
  this is not available.
- Changes to microsandbox itself. The helper uses the existing volume and
  network APIs.
- Sharing one OAuth session between host and sandboxes. Per-project sessions
  replace sharing.

## Approach

The login helper runs on the host. It computes the project's persistent volume
name (same `basename + sha256[:8]` rule as `run.sh`), runs the standard Codex
authorization-code flow against a temporary home, and writes the credential file
into the volume. The vendored OAuth flow matches the fork's behavior: PKCE,
state validation, the fixed `localhost:1455` callback, and the manual paste
fallback when no browser or port is available. Because the flow runs on the
host, the browser opens and the callback completes with no paste and no guest
port.

The guest no longer receives the host credential file. Tau in the guest reads
and writes the project-local credential with its normal atomic writer. Token
refresh happens inside the guest against the project-local file. A cross-process
lock (a sibling `flock` file) serializes refresh across processes that share the
volume, so a rotating token is spent at most once even when two sandboxes run on
the same project.

The credential format stays exactly what Tau already reads and writes, so the
helper produces files the guest consumes without any guest change beyond the
mount removal.

Alternatives considered: keeping the shared file with locks (rejected; rotation
is not shareable across isolated refreshers), publishing the callback port
(rejected; fixed port allows only one sandbox at a time and opens the inbound
boundary), and a paste-only guest login (kept as the helper's headless fallback
rather than the primary path).

## Impact

- `run.sh`: remove the credential mount and the `TAU_SANDBOX_SHARED_CREDENTIALS`
  shared path; always run with project-local credentials.
- `config/tau-wrapper.py`: remove the in-place `_save` patch; keep only the
  `--append-system-prompt` injection.
- `config/entrypoint.sh`: the existing branch that removes the shared-credential
  symlink when the shared flag is off becomes the normal path.
- New `lib/tau-login-openai` host helper plus `install.sh` linkage to
  `~/.local/bin`.
- `README.md`, `docs/SPEC.md`, `config/APPEND_SYSTEM.md`: per-project credential
  model replaces the shared-credential exception.
- Tau fork (`naripok/tau`): cross-process refresh lock; `TAU_REF` in the
  `Containerfile` bumps to the commit that carries it.
