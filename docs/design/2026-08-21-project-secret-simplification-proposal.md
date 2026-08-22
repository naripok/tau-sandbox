# Proposal: Project-Secret Simplification

## Intent

`lib/project-secrets.sh` is 2,279 lines (63 functions, 52 error classes) with a
3,789-line test file, and its maintainer — the only user of the sandbox on a
self-configured, single-user host — cannot maintain it. Most of that code
defends against adversaries that do not exist in this threat model (host
processes racing binary replacement, malicious trusted env config) or
re-implements validation the runtime already performs (`msb` parses and
validates `--secret-conf` YAML itself). The goal is the least code that still
injects secrets via `msb --secret-conf` from the paired sibling files.

## Scope

**In scope:**

- Rewrite `lib/project-secrets.sh` as a small pass-through library (~120 lines):
  physical mapping to `$HOME/.<rel>/`, both-or-neither pair check, symlink-escape
  check, reserved-name grep, source `secrets.env`, pass `secrets.yaml` to
  `msb run --secret-conf` unchanged.
- Simplify `run.sh` accordingly (one call site after `TAU_ENV_FILE` sourcing;
  plain `msb` from PATH; no pinning/freeze/lifecycle ordering).
- Rewrite `tests/test_project_secrets.py` to match; update `tests/test_run.py`,
  `tests/conftest.py`, and `tests/test_integration.py` where they reference
  removed behavior.
- Collapse the secret-domain requirements in `docs/SPEC.md` and shrink the
  README/`APPEND_SYSTEM.md` sections to the new behavior.

**Out of scope:**

- Changing `config/entrypoint.sh` (the `TAU_ENTRYPOINT_*` namespacing is already
  merged, harmless, and stays).
- Re-adding any adversarial hardening (binary pinning, exposure walkers, freeze
  machinery, strict self-parsed grammars, version gate).
- Changing unrelated launcher behavior (image build, packages, reset, mounts).

## Approach

Pass-through, sourced values, basic sanity checks:

1. **Mapping (unchanged logic):** physical launch path strictly below physical
   projects root → `$HOME/.<rel>/`; default root `${HOME}/Projects` disables
   silently when absent; explicit `TAU_PROJECTS_DIR` must be a directory.
2. **Pair sanity:** `secrets.env` and `secrets.yaml` must both be present,
   readable regular files or both absent; a mixed pair fails. The resolved
   secret directory must not lie inside the projects root (symlink escape).
3. **Load:** after `TAU_ENV_FILE` sourcing, `set -a; source secrets.env; set +a`
   (trusted dotenv-style shell). Names are extracted with one `sed` line for the
   two checks below. Secret values therefore win over same-named env-file
   assignments in the launcher environment that `msb` inherits.
4. **Checks:** a name matching the compact reserved ERE (shell/runtime-critical
   names, `BASH*`, `TAU_*`) fails the launch; a name declared as a secret is
   suppressed from `-e` raw forwarding.
5. **Injection:** exactly one `--secret-conf <path to secrets.yaml>` is inserted
   immediately after `run` in the final `msb` argv. `msb` expands `${NAME}`
   references from its inherited environment and delivers `$MSB_<NAME>`
   placeholders to the guest; destination-scoped substitution, DNS/TLS/authority
   handling, and violation behavior are entirely msb's documented contract.

Values never enter argv, mounts, snapshots, or images — they exist only in the
launcher/`msb` process environment.

## Alternatives considered

- **Mechanical compaction only** (~-30%): keeps the same concepts; rejected —
  the maintenance burden is conceptual, not typographic.
- **Keep strict self-parsed grammars** (~600 LoC): rejected — msb already
  validates the policy; self-parsing only added a parallel contract to maintain.
- **Python rewrite:** rejected — needs the same trust scaffolding around a much
  larger interpreter surface.

## Impact

- `lib/project-secrets.sh`: 2,279 → ~120 lines; 63 → ~6 functions; 52 → ~4
  error messages.
- `tests/test_project_secrets.py`: 3,789 → ~500 lines; net deletion across the
  repo ≈ 5,500 lines.
- `docs/SPEC.md`: 12 secret-domain requirements collapse to 4.
- Behavioral concessions (accepted, single-user trusted host): no mid-launch
  binary-swap defense, no hard-link hunts, no freeze/POSIX isolation, `secrets.env`
  becomes executable shell config, policy errors surface from `msb` at launch,
  no msb version gate (old msb fails naturally on the unknown flag).
