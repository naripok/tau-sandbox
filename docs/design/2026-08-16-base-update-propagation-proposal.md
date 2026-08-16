# Proposal: Base-Update Propagation to Per-Project Package Images

## Intent

The sandbox image is the upgrade vehicle for Tau: the `Containerfile` pins `TAU_REF`
and `make build` refreshes the shared `tau-agent-isolated:latest` image in the
microsandbox cache. Projects without `.tau-packages` pick the refreshed base up on
their next run.

Projects with `.tau-packages` do not. Their image tag
`tau-agent-isolated-<project>-<package-hash>:latest` depends only on the
`.tau-packages` content, so a base rebuild never invalidates it: `run.sh` boots the
stale cached image forever, regardless of restarts. This was confirmed live when a
`TAU_REF` bump propagated to every project except one that uses `.tau-packages`.

We need base updates to reach package projects through the same approval-gated
rebuild flow that `.tau-packages` changes already use.

## Scope

**In scope:**

- Per-project image tags embed a hash of the base build context (`Containerfile` +
  every regular file directly under `config/`), so any base-input change
  invalidates the tag.
- `run.sh` prunes superseded same-project images from the msb cache when it rebuilds
  a per-project image, so base bumps do not accumulate orphaned image layers.
- Update `docs/SPEC.md` (living spec), `README.md`, the `Containerfile` upgrade
  comment, and `config/APPEND_SYSTEM.md`.
- Update `tests/test_run.py` and add regression tests for the stale-base scenario.

**Out of scope:**

- `archlinux:latest` / pacman package drift as a standalone rebuild trigger. A
  rebuild is triggered by any base-input change; the rebuild then pulls the current
  base image and pacman packages at build time. Forcing a refresh without input
  changes remains a manual `msb rmi` of the per-project tag.
- Changes to the shared-base update flow (`make build` + `msb load`).
- Changes to the `TAU_IMAGE` bypass behavior.
- Pruning the host-side podman image store (only the msb cache is managed).

## Approach

**Recommended: content hash of the build context in the per-project tag.**

`run.sh` computes `BASE_HASH` — the first 8 hex characters of the SHA-256 of the
hex-encoded SHA-256 digests (64 lowercase hex chars each, concatenated with no
separators) of the `Containerfile` and every regular file directly under `config/`
(sorted by path; dotfiles like `config/.bashrc` included) — and names package
images `tau-agent-isolated-<project>-<BASE_HASH>-<PKG_HASH>`.

When the base changes (`TAU_REF` bump, wrapper/entrypoint/config edits), `BASE_HASH`
changes, the tag is absent from `msb images -q`, and the existing interactive
approval path builds the new image (podman's layer cache busts the `RUN pip install
tau@$TAU_REF` layer because the `ARG` value it consumes changed) and loads it via
`podman save | msb load`. Non-interactive runs keep refusing to rebuild; `TAU_IMAGE`
still bypasses everything.

On that rebuild, `run.sh` also prunes any other cached image matching
`localhost/tau-agent-isolated-<project>-<8hex>:latest` (legacy single-hash form)
or `localhost/tau-agent-isolated-<project>-<8hex>-<8hex>:latest` (two-hash form),
excluding the fresh ref. `msb rmi` failures are ignored — they must never fail the
build, load, or launch. This covers both tags orphaned by base bumps and legacy
pre-change tags.

Hash inputs: only regular files are hashed, so directories that appear under
`config/` (e.g. `__pycache__/`) are ignored deterministically. A missing
`Containerfile` or `config/` directory aborts loudly under `set -e` rather than
silently launching an image whose freshness cannot be verified.

**Alternatives considered:**

- *Thin overlay on a split base* (per-project image `FROM` the shared base tag;
  rebuild when the host podman base image ID changes). Faster rebuilds, but adds a
  podman dependency to every cached run of a package project, needs a fallback when
  the host image is absent, and splits the image layout. Rejected for complexity.
- *Manual `msb rmi` update step.* No code change, but re-introduces exactly the
  failure the user hit. Rejected.

## Impact

- `run.sh`: tag derivation in the package branch; prune step in the build branch.
- `tests/test_run.py`: updated package-build assertion; two new tests (stale tag
  rebuilds + prunes, current tag skips build and prune).
- `docs/SPEC.md`: modified "Per-project package declarations" requirement; new
  pruning requirement.
- `README.md` (`.tau-packages` section and How It Works), `Containerfile` comment,
  `config/APPEND_SYSTEM.md` (agents learn base updates trigger the same approval).
