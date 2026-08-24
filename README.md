# Tau Agent Isolation Environment

Per-project isolation for the [Tau coding agent](https://github.com/huggingface/tau) using [microsandbox](https://github.com/superradcompany/microsandbox) microVMs.

Each project runs in its own hardware-isolated microVM. The microVM has persistent home, session, and log volumes. Installed tools, sandbox-only Tau config, and shell customizations survive across runs. Tau sessions and diagnostics stay isolated per project.

The launcher synchronizes host Tau config into the sandbox at every start. The host files are never modified. `credentials.json` is the sole write exception, for OAuth refresh. API credentials configured as [protected project secrets](#protected-project-secrets) reach the guest only as placeholders. The runtime substitutes the real values only for explicitly allowed destinations.

## The Problem

AI coding agents execute arbitrary shell commands, read files, and install packages. Running them directly on the host means:

- An agent on `project-a` can read secrets from `project-b`.
- A compromised npm package can access SSH keys, dotfiles, and every project on the machine.
- Sessions, installed tools, and config are lost every time the sandbox exits.

This project solves all three problems with a microVM-per-project model. Persistent named volumes back the model. Containers share the host kernel. Every Tau session here runs inside a hardware-isolated microVM with its own kernel and network stack.

## Quick Start

```bash
git clone https://github.com/naripok/tau-sandbox.git ~/tau-sandbox
cd ~/tau-sandbox && ./install.sh
```

The setup requires the [microsandbox](https://docs.microsandbox.dev/quickstart) CLI (`msb`). podman is only for image builds.

Add the printed alias to your `~/.bashrc` or `~/.zshrc`. Then use it from any project:

```bash
cd ~/Projects/my-project
tau-sandbox tau -p "Review this codebase"   # run tau in one-shot mode
tau-sandbox tau                             # start the Tau TUI
tau-sandbox bash                            # interactive shell
tau-sandbox npm test                        # any command inside
tau-sandbox --reset                         # wipe per-project persistent state
```

The first run of `tau-sandbox` builds the Arch Linux image. It loads the image into the microsandbox cache. Subsequent runs boot in less than a second.

## Per-Project System Dependencies

The sandbox image ships with a fixed set of tools: Python, uv, Node.js, git, gcc, and more. If your project needs additional system-level packages (CMake, libffi, ffmpeg), declare them in a `.tau-packages` file in the project root:

```
# Build tools
cmake
pkgconf

# Cryptography
libffi
```

**How it works:**

1. Create `.tau-packages` in your project root. Use one Arch package per line. `#` starts a comment.
2. On the next `tau-sandbox` run, the launcher finds the file. Then it prompts you to approve the packages.
3. On approval, the launcher builds a per-project image with those packages. The launcher loads the image into the microsandbox cache.
4. The image name includes hashes of `.tau-packages` and of the sandbox base inputs (`Containerfile` and `config/`). Changes to the packages or the base, for example Tau upgrades, trigger a new approval and rebuild.

Projects without `.tau-packages` use the shared base image. No extra rebuild is needed.

**Security:** Every change to `.tau-packages` requires explicit user approval before the launcher rebuilds the image. The agent can write `.tau-packages` but cannot bypass the approval gate. Non-interactive mode (pipes, CI) refuses to rebuild without approval.

The same gate covers base updates. Per-project images embed a hash of the base inputs. A base rebuild invalidates these images. The next interactive run asks for approval again.

Rebuilds prune superseded images of the current package content from the microsandbox cache. Images of other package contents, for example same-image-name projects or earlier `.tau-packages` contents, are kept.

**Override:** Set `TAU_IMAGE=my-image-ref` to bypass `.tau-packages` and automatic image management. Use a specific image. Load it yourself, for example with `make build`.

## Protected Project Secrets

API credentials can reach a sandbox without their real values ever entering the guest. When a launch directory belongs to a projects root, the launcher looks for a paired `secrets.env` / `secrets.yaml` in a hidden host-only directory. The launcher sources the values into the runtime's environment. The launcher hands the policy file to the microsandbox runtime unmodified via `msb run --secret-conf`. The guest receives only placeholders. The runtime substitutes real values only for the destinations and request locations that each secret's policy allows.

### Mapping: launch directory → secret location

`TAU_PROJECTS_DIR` selects the projects root:

- Unset. The default is `${HOME}/Projects`. When the default root is absent or not usable, the launcher disables project-secret discovery.
- Explicitly set. An explicitly empty value is invalid. A relative value resolves from the launch directory. An explicit root that is dangling, a non-directory, unreadable, or unsearchable fails the launch.

A launch directory that is a proper descendant of the physical projects root maps to a hidden directory under your home. The launcher prefixes the first relative component with a dot:

| Launch directory | Secret directory |
| --- | --- |
| `$HOME/Projects/megali` | `$HOME/.megali` |
| `$HOME/Projects/megali/main` | `$HOME/.megali/main` |
| `$HOME/Projects/megali/main/api` | `$HOME/.megali/main/api` |

The mapping is exact. Nested launches never inherit the parent's secrets. Configurations never merge. Launching from the projects root itself, or from a directory outside it, selects no project secrets.

The secret directory must stay outside the projects root. The launcher rejects a mapped directory that physically resolves inside the projects root. This prevents a symlink escape. Secret sources can never live on mounted project data.

### Paired sources: `secrets.env` and `secrets.yaml`

Project secrets live in exactly two files in the mapped directory. `secrets.env` holds the values. `secrets.yaml` holds the policy. Both files must be present and readable, or both absent. An incomplete pair fails the launch.

The launcher sources `secrets.env` as shell. Like `.env`, it is trusted host config. Plain `NAME=VALUE` or `export NAME=VALUE` assignments are allowed. Shell quoting is allowed. The launcher exports every assigned name into the runtime's environment after it sources `TAU_ENV_FILE`. Secret values win over same-named ordinary assignments.

`secrets.yaml` uses the microsandbox runtime's native `--secret-conf` format. The launcher passes the file to the runtime unmodified. The launcher never parses, validates, or rewrites it. The `msb` runtime reports policy errors at launch.

Example `$HOME/.megali/secrets.env`:

```bash
OPENAI_API_KEY=sk-proj-REPLACE-WITH-REAL-KEY
STRIPE_API_KEY=sk-test-REPLACE-WITH-REAL-KEY
```

Example `$HOME/.megali/secrets.yaml`:

```yaml
OPENAI_API_KEY:
  value: "${OPENAI_API_KEY}"
  allow:
    - api.openai.com
  inject:
    - headers
STRIPE_API_KEY:
  value: "${STRIPE_API_KEY}"
  allow:
    - api.stripe.com
    - "*.stripe.com"
```

Each `value:` references the same-named variable from `secrets.env`. The runtime resolves the reference from its inherited environment. The runtime never forwards the value to the guest.

#### Reserved names

A declared secret name must not be one of the shell- and runtime-critical names (`HOME`, `SHELL`, `TERM`, `COLORTERM`, `USER`, `LOGNAME`, `PATH`, `IFS`, `PWD`, `OLDPWD`, `SHLVL`, `BASH_ENV`, `ENV`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONHOME`, `PYTHONPATH`, `NODE_OPTIONS`) or begin with `BASH` or `TAU_`. The launcher rejects a reserved name before the sandbox is created.

### Runtime requirements

Protected project secrets need an `msb` runtime that supports `run --secret-conf`. A runtime without this flag fails on the unknown flag. The launcher does not detect the runtime version. Launching without a present pair never touches the secret machinery.

### Placeholders and substitution

For each active secret, the guest environment contains a runtime-generated placeholder of the form `$MSB_<NAME>`. The placeholder is never the real value. `env` shows the placeholder.

The sandbox runtime substitutes the real value only for HTTP(S) requests. The destination and the request location (header, basic-auth credential, or query parameter) of the request must be in the secret's policy. DNS observation, TLS identity, HTTP authority, and violation handling match the runtime's documented contract.

Allowing a destination for a secret never grants network access. The destination allowlist does not expand the sandbox network policy. `TAU_LAN_HOSTS` remains the only private-network exception.

### Interaction with env-file forwarding

`TAU_ENV_FILE` remains trusted executable host config. When a name is declared both in the env file and as a project secret, the launcher suppresses the ordinary value from raw forwarding. Source order does not matter. The protected source supplies the guest variable.

## How It Works

```
Host                              Sandbox (microVM)
─────────────────                 ─────────────────
~/Projects/my-project/   ───────► /workspace                  (read-write)
~/.tau/*                 ───────► /etc/tau-sandbox/bootstrap/tau/* (read-only)
                                      │ first project run
                                      ▼
msb home volume          ───────► /home/tau                  (includes rw .tau copy)
~/.tau/credentials.json  ───────► /etc/tau-sandbox/shared/credentials.json (rw)
                                      ▲ linked from ~/.tau/credentials.json
~/.agents/               ───────► /home/tau/.agents          (read-only, optional)
msb sessions volume      ───────► /var/lib/tau-sandbox/sessions (linked from ~/.tau)
msb logs volume          ───────► /var/lib/tau-sandbox/logs  (linked from ~/.tau)
(home volume state)      ───────► ~/.tau/trust.json          (read-write, isolated)
~/.megali/secrets.{env,yaml} ──► --secret-conf → msb (never mounted; guest sees $MSB_* placeholders)

podman image → msb run → boot microVM → entrypoint → tau wrapper → Tau
```

- **One project, one sandbox, isolated state.** Each project gets separate home, session, and diagnostic-log volumes. The resolved path of the project keys the volumes.
- **Ephemeral microVM.** The VM boots with a disposable writable root overlay. `/tmp` is a separate tmpfs. Both are discarded afterwards. Microsandbox has no container-style `--read-only` rootfs switch. The image runs unprivileged instead. The image has no setuid/setgid binaries. The disposable overlay contains root writes.
- **Hardware isolation.** The guest has its own kernel. The host is reachable only through explicit mounts. The host enforces the mounts.
- **Writable project config synchronized from the host.** The entrypoint mounts existing top-level `~/.tau` entries read-only at a bootstrap path. The entrypoint refreshes them into the persistent project home on every start. Tau can atomically update providers, model choices, thinking effort, and other local config during a run. These updates do not change the host. Host-managed entries return to the host version on restart. `~/.agents` remains read-only. `credentials.json` alone stays shared and writable. Rotated OAuth tokens remain valid.
- **Isolated sessions, logs, and trust.** Tau config, history, diagnostics, and project trust decisions persist per project. They do not modify host state.
- **Transparent pair-coding.** The project directory is a bind mount. Your host editor and the sandbox agent see the same files at the same time.
- **Protected project secrets never enter the guest.** The paired sources stay host-only. The launcher hands the runtime the policy file. The guest sees only `$MSB_<NAME>` placeholders. The runtime substitutes real values for policy-allowed requests.

## Architecture

| Component | Description |
| --- | --- |
| `Containerfile` | Arch Linux image with Python, uv, Node.js, tau, and the entrypoint |
| `config/entrypoint.sh` | Synchronizes host config and initializes the persistent sandbox home |
| `config/tau-wrapper.py` | Injects invariant sandbox context and handles mounted credential writes |
| `config/.bashrc` | Shell prompt, aliases, and persistent PATH config |
| `config/APPEND_SYSTEM.md` | Immutable agent environment reference |
| `run.sh` | Launch script — makes sure the image is present, mounts project and state, boots the VM |
| `install.sh` | Prerequisite checks, image build/load, and alias setup |
| `Makefile` | Convenience targets (`build`, `shell`, `tau`, `clean`, `reset`) |
| `tests/` | Pytest suite for build, filesystem, persistence, and integration |

## Agent Environment Awareness

The sandboxed agent knows it is in a microVM. It also knows exactly what it can and cannot do. The system prompt injection tells the agent this explicitly. Nothing is guessed or inferred.

`run.sh` mounts `config/APPEND_SYSTEM.md` read-only at `/etc/tau-sandbox/APPEND_SYSTEM.md`. The image's `tau` wrapper always prepends `--append-system-prompt` with that path. Project or user prompt files cannot shadow the sandbox reference. Host defaults are never overwritten.

Additional explicit `--append-system-prompt` options are combined in command-line order. Tau treats an explicit append input as higher precedence. Automatic user/project `APPEND_SYSTEM.md` discovery is suppressed. When you need another append file, pass it explicitly. The image also contains a fallback copy for direct image use.

## Configuration

All config is controlled via environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `TAU_IMAGE` | `tau-agent-isolated` | Full image reference used by msb. Bypasses `.tau-packages` and automatic build/load. |
| `TAU_CONFIG_DIR` | nearest ancestor `.tau`, else `~/.tau` | Host Tau config refreshed at each start. When unset, the nearest ancestor directory with a `.tau` config dir is used. `credentials.json` remains shared. |
| `TAU_AGENTS_DIR` | `~/.agents` | Host `.agents` resources, mounted read-only at `/home/tau/.agents` |
| `TAU_ENV_FILE` | `~/.env` | Env file whose variables are forwarded into the sandbox |
| `TAU_PROJECTS_DIR` | `${HOME}/Projects` | Projects root for protected project-secret discovery. An explicitly empty value is invalid. A relative value resolves from the launch directory. |
| `TAU_CPUS` | `4` | Virtual CPUs for the sandbox |
| `TAU_MEM` | `8G` | Memory for the sandbox |
| `TAU_PIDS` | `1024` | Process (nproc) limit inside the sandbox |
| `TAU_LAN_HOSTS` | *(none)* | Comma-separated exact-IP LAN hosts allowed egress. When empty, all private addresses stay blocked. |

### Project-local config

When `TAU_CONFIG_DIR` is not set, `run.sh` walks up from the launch directory. It uses the nearest ancestor's `.tau` directory as the host Tau config directory. This per-project config can be a real directory or a symlink to a config world outside the project tree. This mirrors the project-local `.tau-packages` convention. The sandbox adapts to the project you launch it from. `TAU_CONFIG_DIR` always overrides discovery.

### Environment Variables

Variables defined in `~/.env` (or the path set by `TAU_ENV_FILE`) are automatically forwarded into the sandbox as real guest values. This suits ordinary config — feature flags, defaults, mirrors:

```
PIP_DEFAULT_TIMEOUT=60
UV_CONCURRENT_DOWNLOADS=4
```

> **Warning:** raw forwarding is the wrong tool for API keys you intend to protect. A raw-forwarded value is real plaintext inside the guest. Any command the guest runs can read it. Declare such credentials as [protected project secrets](#protected-project-secrets) instead. They reach the guest only as placeholders.

A name that is also declared as a project secret is suppressed from raw forwarding. The protected source supplies the guest variable. `TAU_ENV_FILE` itself remains trusted executable host config.

### Network Access

The launcher uses microsandbox's `public` network profile. Private-network addresses are blocked except for exact hosts listed in `TAU_LAN_HOSTS` (comma-separated IP addresses or hostnames, empty by default):

```bash
TAU_LAN_HOSTS=192.168.1.100 tau-sandbox tau
```

Each listed address is reachable on any port or protocol. All other private-network addresses remain blocked. No guest ports are published, so inbound connections to the sandbox are not possible.

Allowing a destination in a secret's policy never grants network access. Secret destinations do not expand the sandbox network policy. `TAU_LAN_HOSTS` remains the only private-network exception.

### Sandbox Environment Variables

In addition to forwarded host variables, the entrypoint sets sandbox-specific defaults on every boot:

| Variable | Description |
| --- | --- |
| `TAU_NO_UPDATE_CHECK` | Skips Tau's online version check (the image is the update vehicle) |
| `TAU_SANDBOX_SHARED_CREDENTIALS` | Internal `run.sh` marker selecting bind-mount-safe credential writes |

### Sandbox Filesystem

| Path | Source | Permissions |
| --- | --- | --- |
| `/workspace` | Current directory | Read-write |
| `/home/tau/.tau/*` | Per-project home volume | Read-write |
| `/etc/tau-sandbox/bootstrap/tau/*` | Existing host `~/.tau` entries | Read-only |
| `/home/tau/.tau/credentials.json` | Link to shared tokens, or local | Read-write |
| `/etc/tau-sandbox/shared/credentials.json` | Host login tokens, when present | Read-write |
| `/home/tau/.tau/sessions/` | Link to per-project volume | Read-write |
| `/home/tau/.tau/logs/` | Link to per-project volume | Read-write |
| `/var/lib/tau-sandbox/sessions/` | Sessions volume backing path | Read-write |
| `/var/lib/tau-sandbox/logs/` | Logs volume backing path | Read-write |
| `/home/tau/.tau/trust.json` | Per-project home volume | Read-write |
| `/home/tau/.agents/` | Host `~/.agents/` (if present) | Read-only |
| `/home/tau` | Per-project home volume | Read-write |
| `/home/tau/.local/` | User-level package installs | Read-write |
| `/tmp` | Per-run tmpfs | Read-write |

The `/workspace` mount uses `host-perms=mirror`. Files and directories created or chmod'd inside the sandbox keep their rwx bits on the host inode. Scripts stay executable. Git's exec-bit tracking stays consistent. Only ordinary rwx bits are mirrored. Ownership, file type, and setuid/setgid are not mirrored. An owner-access floor always applies.

Other exports keep the sandbox's default private metadata policy. Under this policy, guest-created files appear on the host as owner-only (`600`/`700`).

Project-secret sources (`~/.<project>/secrets.env` and `secrets.yaml`) are never mounted, copied, snapshotted, or built into the image. They stay host-only. Only the policy path reaches the runtime.

### Host Config and Isolated State

`run.sh` copies existing regular top-level host `~/.tau` files and directories into a temporary host-side snapshot. The snapshot excludes credentials, sessions, logs, trust-store files, and internal synchronization metadata. `run.sh` dereferences symlinks at every depth while the host paths are available. Linked skills, extensions, themes, prompts, and other config become ordinary snapshot files and directories. Dangling links cause startup to fail. They do not silently install broken resources.

Snapshot entries are mounted individually and read-only under `/etc/tau-sandbox/bootstrap/tau`. On every start, the entrypoint replaces host-managed project copies with the current host versions. The entrypoint also removes resources deleted from the host. Config created only inside the sandbox remains persistent. Sandbox edits to host-managed config are writable during a run. The next start replaces them from the host.

This separate source and destination layout is required for Tau's atomic config writes. Tau writes a sibling temporary file and renames it over files such as `providers.json`. A file bind mount is itself a mount point. Linux rejects the replacement with `EBUSY`. The project-local copy is on one writable filesystem, so atomic replacement works. Model, provider, scoped-model, and thinking-effort changes persist normally.

`credentials.json` is mounted read-write as a deliberate exception. Tau normally updates credentials by atomically replacing the file. File mounts cannot support this. The sandbox wrapper uses a bind-mount-safe in-place writer only when shared credentials are mounted. If the host credential file is absent, Tau can create project-local credentials in the persistent home instead.

Microsandbox creates nested mount targets as root before launching the configured user. The launcher mounts shared credentials and the isolated session/log volumes at backing paths outside `/home/tau`. A new persistent `~/.tau` stays owned by UID 1000. The entrypoint creates links at Tau's normal paths. Mount setup cannot create an unwritable `~/.tau` before startup synchronization. Existing non-empty session or log directories from the earlier layout are merged into the named volumes. The merge does not overwrite canonical volume data.

Host sessions and logs are excluded and replaced by per-project named volumes. Trust-store files live in the per-project home. Tau needs a writable lock and atomic updates for them. Host trust paths do not match the guest's canonical `/workspace` path. `~/.agents` remains mounted read-only.

## Security Model

| Threat | Mitigation |
| --- | --- |
| Agent reads other projects | Only the current directory is mounted as `/workspace` |
| Agent modifies host Tau config | Tau config is exposed through read-only dereferenced snapshots. `.agents` is read-only. Writable config is project-local. Only `credentials.json` is shared, for login and OAuth refresh. |
| Agent modifies host history/trust | Sessions, diagnostic logs, and trust decisions use isolated per-project state |
| Agent escapes to host filesystem | Hardware-isolated microVM. Mounts are brokered host-side with path containment and identity virtualization. |
| Agent escalates to root in guest | Runs as unprivileged `tau` (UID 1000). Uses the `restricted` profile. Has no setuid/setgid image binaries. |
| Agent modifies the image rootfs | Root writes are permission-limited. The writable overlay is discarded after every run. `/tmp` is a separate tmpfs. |
| Network exfiltration | Public internet, gateway DNS, and hosts listed in `TAU_LAN_HOSTS` are allowed for egress. Other private addresses and all unpublished inbound traffic are denied. |
| Persistent volume as attack vector | The volume is microsandbox-managed, not a host bind mount. Intra-project persistence of malicious files is possible but contained. |
| Secrets leak through images | Credentials are never baked into the image. Protected project secrets reach the guest only as runtime placeholders. |
| Agent reads protected API keys | Project secrets appear in the guest only as `$MSB_<NAME>` placeholders. The runtime substitutes real values only for the policy-allowed destinations and request locations of each secret. |

## Reset

```bash
./run.sh --reset
```

This removes the project's home, session, and log volumes. Installed tools, isolated history, custom `.bashrc` edits, and other per-project state are removed. Host `~/.tau`, `~/.agents`, and credentials remain.

`--reset` bypasses secret discovery entirely. It removes the volumes without reading or validating the project-secret sources. Invalid secrets never block a reset.

## Testing

```bash
uv run pytest tests/
```

The test suite covers:

- **Unit tests** — script existence and syntax, Containerfile directives, Makefile targets, config files, run.sh flag generation, package-approval flow.
- **Integration tests** — image build/load, filesystem layout, host-config synchronization, atomic provider writes, credential writes, isolated sessions/logs, persistence, and volume isolation.
- **Security tests** — security flags, mount allowlist, dangerous-character rejection.
- **Project-secret tests** — exact-directory mapping, paired-source sanity checks and reserved names, placeholder injection, and forwarding precedence.

Integration tests build the image once per session. They require `msb` and podman. The tests are skipped automatically when either tool is not available.

## Requirements

- [microsandbox](https://docs.microsandbox.dev/quickstart) CLI. Linux needs KVM. macOS needs Apple Silicon. Protected project secrets need an `msb` runtime that supports `run --secret-conf`. This applies only when a `secrets.env`/`secrets.yaml` pair is present.
- podman. It is used only to build the OCI image.
- Bash 4+

## See Also

- [docs/SPEC.md](docs/SPEC.md) — Behavioral specification
- [microsandbox](https://github.com/superradcompany/microsandbox) — microVM runtime
- [Tau](https://github.com/huggingface/tau) — the coding agent
- [pi-sandbox](https://github.com/naripok/pi-sandbox) — the container-based predecessor this project mirrors
