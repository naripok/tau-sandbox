# opencode Agent Isolation Environment

Per-project isolation for the [opencode coding agent](https://opencode.ai) using [microsandbox](https://github.com/superradcompany/microsandbox) microVMs.

Each project runs in its own hardware-isolated microVM. The microVM has persistent home, session, and log volumes. Installed tools, sandbox-only opencode config, and shell customizations survive across runs. opencode sessions and diagnostics stay isolated per project.

The launcher synchronizes host opencode config into the sandbox at every start. The host files are never modified. Each sandbox keeps its own `auth.json` inside the persistent project volume. The host `~/.local/share/opencode/auth.json` is never mounted. The host helper `opencode-login-openai PROJECT_PATH` runs the OpenAI login on the host and writes the credential into the project volume. API credentials configured as [protected project secrets](#protected-project-secrets) reach the guest only as placeholders. The runtime substitutes the real values only for explicitly allowed destinations.

## The Problem

AI coding agents execute arbitrary shell commands, read files, and install packages. Running them directly on the host means:

- An agent on `project-a` can read secrets from `project-b`.
- A compromised npm package can access SSH keys, dotfiles, and every project on the machine.
- Sessions, installed tools, and config are lost every time the sandbox exits.

This project solves all three problems with a microVM-per-project model. Persistent named volumes back the model. Containers share the host kernel. Every opencode session here runs inside a hardware-isolated microVM with its own kernel and network stack.

## Quick Start

```bash
git clone https://github.com/<you>/opencode-sandbox.git ~/opencode-sandbox
cd ~/opencode-sandbox && ./install.sh
```

The setup requires the [microsandbox](https://docs.microsandbox.dev/quickstart) CLI (`msb`). podman is only for image builds.

Add the printed alias to your `~/.bashrc` or `~/.zshrc`. Then use it from any project:

```bash
cd ~/Projects/my-project
opencode-sandbox opencode run "Review this codebase"   # run opencode in one-shot mode
opencode-sandbox opencode                              # start the opencode TUI
opencode-sandbox bash                                  # interactive shell
opencode-sandbox npm test                              # any command inside
opencode-sandbox --reset                               # wipe per-project persistent state
```

The first run of `opencode-sandbox` builds the Arch Linux image. It loads the image into the microsandbox cache. Subsequent runs boot in less than a second.

## Per-Project System Dependencies

The sandbox image ships with a fixed set of tools: Python, uv, Node.js, git, gcc, and more. The image also contains the pinned opencode release. If your project needs additional system-level packages (CMake, libffi, ffmpeg), declare them in a `.opencode-packages` file in the project root:

```
# Build tools
cmake
pkgconf

# Cryptography
libffi
```

**How it works:**

1. Create `.opencode-packages` in your project root. Use one Arch package per line. `#` starts a comment.
2. On the next `opencode-sandbox` run, the launcher finds the file. Then it prompts you to approve the packages.
3. On approval, the launcher builds a per-project image with those packages. The launcher loads the image into the microsandbox cache.
4. The image name includes hashes of `.opencode-packages` and of the sandbox base inputs (`Containerfile` and `config/`). Changes to the packages or the base, for example an opencode upgrade, trigger a new approval and rebuild.

Projects without `.opencode-packages` use the shared base image. No extra rebuild is needed.

**Security:** Every change to `.opencode-packages` requires explicit user approval before the launcher rebuilds the image. The agent can write `.opencode-packages` but cannot bypass the approval gate. Non-interactive mode (pipes, CI) refuses to rebuild without approval.

The same gate covers base updates. Per-project images embed a hash of the base inputs. A base rebuild invalidates these images. The next interactive run asks for approval again.

Rebuilds prune superseded images of the current package content from the microsandbox cache. Images of other package contents, for example same-image-name projects or earlier `.opencode-packages` contents, are kept.

**Override:** Set `OPENCODE_SANDBOX_IMAGE=my-image-ref` to bypass `.opencode-packages` and automatic image management. Use a specific image. Load it yourself, for example with `make build`.

## Protected Project Secrets

API credentials can reach a sandbox without their real values ever entering the guest. When a launch directory belongs to a projects root, the launcher looks for a paired `secrets.env` / `secrets.yaml` in a hidden host-only directory. The launcher sources the values into the runtime's environment. The launcher hands the policy file to the microsandbox runtime unmodified via `msb run --secret-conf`. The guest receives only placeholders. The runtime substitutes real values only for the destinations and request locations that each secret's policy allows.

### Mapping: launch directory → secret location

`OPENCODE_SANDBOX_PROJECTS_DIR` selects the projects root:

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

The launcher sources `secrets.env` as shell. Like `.env`, it is trusted host config. Plain `NAME=VALUE` or `export NAME=VALUE` assignments are allowed. Shell quoting is allowed. The launcher exports every assigned name into the runtime's environment after it sources `OPENCODE_SANDBOX_ENV_FILE`. Secret values win over same-named ordinary assignments.

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

A declared secret name must not be one of the shell- and runtime-critical names (`HOME`, `SHELL`, `TERM`, `COLORTERM`, `USER`, `LOGNAME`, `PATH`, `IFS`, `PWD`, `OLDPWD`, `SHLVL`, `BASH_ENV`, `ENV`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONHOME`, `PYTHONPATH`, `NODE_OPTIONS`) or begin with `BASH` or `OPENCODE_SANDBOX_`. The launcher rejects a reserved name before the sandbox is created.

### Runtime requirements

Protected project secrets need an `msb` runtime that supports `run --secret-conf`. A runtime without this flag fails on the unknown flag. The launcher does not detect the runtime version. Launching without a present pair never touches the secret machinery.

### Placeholders and substitution

For each active secret, the guest environment contains a runtime-generated placeholder of the form `$MSB_<NAME>`. The placeholder is never the real value. `env` shows the placeholder.

The sandbox runtime substitutes the real value only for HTTP(S) requests. The destination and the request location (header, basic-auth credential, or query parameter) of the request must be in the secret's policy. DNS observation, TLS identity, HTTP authority, and violation handling match the runtime's documented contract.

Allowing a destination for a secret never grants network access. The destination allowlist does not expand the sandbox network policy. `OPENCODE_SANDBOX_LAN_HOSTS` remains the only private-network exception.

### Interaction with env-file forwarding

`OPENCODE_SANDBOX_ENV_FILE` remains trusted executable host config. When a name is declared both in the env file and as a project secret, the launcher suppresses the ordinary value from raw forwarding. Source order does not matter. The protected source supplies the guest variable.

## How It Works

```
Host                              Sandbox (microVM)
─────────────────                 ─────────────────
~/Projects/my-project/   ───────► /workspace                  (read-write)
~/.config/opencode/*     ───────► /etc/opencode-sandbox/bootstrap/opencode/* (read-only)
                                      │ first project run
                                      ▼
msb home volume          ───────► /home/opencode             (includes rw config copy)
msb sessions volume      ───────► /var/lib/opencode-sandbox/sessions (linked from the data dir)
msb logs volume          ───────► /var/lib/opencode-sandbox/logs (linked from the data dir)
(home volume state)      ───────► ~/.local/share/opencode/auth.json (project-local rw)
~/.megali/secrets.{env,yaml} ──► --secret-conf → msb (never mounted; guest sees $MSB_* placeholders)

podman image → msb run → boot microVM → entrypoint → opencode wrapper → opencode
```

- **One project, one sandbox, isolated state.** Each project gets separate home, session-storage, and diagnostic-log volumes. The resolved path of the project keys the volumes.
- **Ephemeral microVM.** The VM boots with a disposable writable root overlay. `/tmp` is a separate tmpfs. Both are discarded afterwards. Microsandbox has no container-style `--read-only` rootfs switch. The image runs unprivileged instead. The image has no setuid/setgid binaries. The disposable overlay contains root writes.
- **Hardware isolation.** The guest has its own kernel. The host is reachable only through explicit mounts. The host enforces the mounts.
- **Writable project config synchronized from the host.** The entrypoint mounts existing top-level host config entries read-only at a bootstrap path. The entrypoint refreshes them into the persistent project home on every start. opencode can update providers, model choices, agents, and other local config during a run. These updates do not change the host. Host-managed entries return to the host version on restart. `auth.json` stays project-local in the persistent home. Rotated OAuth tokens remain valid inside the project volume.
- **Isolated sessions and logs.** opencode session storage and diagnostics persist per project in the linked volumes. They do not modify host state.
- **Transparent pair-coding.** The project directory is a bind mount. Your host editor and the sandbox agent see the same files at the same time.
- **Protected project secrets never enter the guest.** The paired sources stay host-only. The launcher hands the runtime the policy file. The guest sees only `$MSB_<NAME>` placeholders. The runtime substitutes real values for policy-allowed requests.

## Architecture

| Component | Description |
| --- | --- |
| `Containerfile` | Arch Linux image with Python, uv, Node.js, the pinned opencode release, and the entrypoint |
| `config/entrypoint.sh` | Synchronizes host config and initializes the persistent sandbox home |
| `config/opencode-wrapper.sh` | Merges the immutable sandbox config into every opencode launch |
| `config/opencode.json` | The sandbox config: instructions entry for the sandbox context, auto-update off |
| `config/.bashrc` | Shell prompt, aliases, and persistent PATH config |
| `config/APPEND_SYSTEM.md` | Immutable agent environment reference |
| `run.sh` | Launch script — makes sure the image is present, mounts project and state, boots the VM |
| `install.sh` | Prerequisite checks, image build/load, and alias setup |
| `lib/opencode-login-openai` | Host helper that runs the OpenAI login and writes the project credential |
| `Makefile` | Convenience targets (`build`, `shell`, `opencode`, `clean`, `reset`) |
| `tests/` | Pytest suite for build, filesystem, persistence, and integration |

## Agent Environment Awareness

The sandboxed agent knows it is in a microVM. It also knows exactly what it can and cannot do. The system prompt injection tells the agent this explicitly. Nothing is guessed or inferred.

`run.sh` mounts `config/opencode.json` and `config/APPEND_SYSTEM.md` read-only at `/etc/opencode-sandbox/`. The image's `opencode` wrapper exports `OPENCODE_CONFIG` with that config path on every launch. The sandbox config carries the sandbox context document as its `instructions` entry. opencode concatenates `instructions` arrays across config sources with deduplication, so global and project config can add instructions but cannot remove the sandbox reference. Host defaults are never overwritten. The image also contains a fallback copy of both files for direct image use.

## Configuration

All config is controlled via environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `OPENCODE_SANDBOX_IMAGE` | `opencode-agent-isolated` | Full image reference used by msb. Bypasses `.opencode-packages` and automatic build/load. |
| `OPENCODE_SANDBOX_CONFIG_DIR` | nearest ancestor `.opencode`, else `${XDG_CONFIG_HOME:-~/.config}/opencode` | Host opencode config refreshed at each start. When unset, the nearest ancestor directory with an `.opencode` config dir is used. `auth.json` stays project-local. |
| `OPENCODE_SANDBOX_ENV_FILE` | `~/.env` | Env file whose variables are forwarded into the sandbox |
| `OPENCODE_SANDBOX_PROJECTS_DIR` | `${HOME}/Projects` | Projects root for protected project-secret discovery. An explicitly empty value is invalid. A relative value resolves from the launch directory. |
| `OPENCODE_SANDBOX_CPUS` | `4` | Virtual CPUs for the sandbox |
| `OPENCODE_SANDBOX_MEM` | `8G` | Memory for the sandbox |
| `OPENCODE_SANDBOX_PIDS` | `1024` | Process (nproc) limit inside the sandbox |
| `OPENCODE_SANDBOX_LAN_HOSTS` | *(none)* | Comma-separated exact-IP LAN hosts allowed egress. When empty, all private addresses stay blocked. |

### Project-local config

When `OPENCODE_SANDBOX_CONFIG_DIR` is not set, `run.sh` walks up from the launch directory. It uses the nearest ancestor's `.opencode` directory as the host opencode config directory. This per-project config can be a real directory or a symlink to a config world outside the project tree. This mirrors the project-local `.opencode-packages` convention. The sandbox adapts to the project you launch it from. `OPENCODE_SANDBOX_CONFIG_DIR` always overrides discovery.

opencode itself also reads `<project>/.opencode` directories inside the workspace natively. The launcher therefore skips a discovered `.opencode` at the launch directory or below it: that config is project config opencode already reads from `/workspace`, and syncing it into the guest global config would load it in two scopes. The discovery sync exists for config that lives outside the launch directory, for example a symlinked config world.

### Environment Variables

Variables defined in `~/.env` (or the path set by `OPENCODE_SANDBOX_ENV_FILE`) are automatically forwarded into the sandbox as real guest values. This suits ordinary config — feature flags, defaults, mirrors:

```
PIP_DEFAULT_TIMEOUT=60
UV_CONCURRENT_DOWNLOADS=4
```

> **Warning:** raw forwarding is the wrong tool for API keys you intend to protect. A raw-forwarded value is real plaintext inside the guest. Any command the guest runs can read it. Declare such credentials as [protected project secrets](#protected-project-secrets) instead. They reach the guest only as placeholders.

A name that is also declared as a project secret is suppressed from raw forwarding. The protected source supplies the guest variable. `OPENCODE_SANDBOX_ENV_FILE` itself remains trusted executable host config.

### Network Access

The launcher uses microsandbox's `public` network profile. Private-network addresses are blocked except for exact hosts listed in `OPENCODE_SANDBOX_LAN_HOSTS` (comma-separated IP addresses or hostnames, empty by default):

```bash
OPENCODE_SANDBOX_LAN_HOSTS=192.168.1.100 opencode-sandbox opencode
```

Each listed address is reachable on any port or protocol. All other private-network addresses remain blocked. No guest ports are published, so inbound connections to the sandbox are not possible.

Allowing a destination in a secret's policy never grants network access. Secret destinations do not expand the sandbox network policy. `OPENCODE_SANDBOX_LAN_HOSTS` remains the only private-network exception.

### Sandbox Environment Variables

In addition to forwarded host variables, the entrypoint and wrapper set sandbox-specific defaults on every boot:

| Variable | Description |
| --- | --- |
| `OPENCODE_CONFIG` | Points opencode at the immutable sandbox config (the wrapper sets it) |
| `OPENCODE_DISABLE_AUTOUPDATE` | Skips opencode's auto-update (the image is the update vehicle) |

### Sandbox Filesystem

| Path | Source | Permissions |
| --- | --- | --- |
| `/workspace` | Current directory | Read-write |
| `/home/opencode` | Per-project home volume | Read-write |
| `/home/opencode/.config/opencode/*` | Per-project home volume | Read-write |
| `/etc/opencode-sandbox/bootstrap/opencode/*` | Existing host config entries | Read-only |
| `/home/opencode/.local/share/opencode/auth.json` | Per-project home volume | Read-write |
| `/home/opencode/.local/share/opencode/storage/` | Link to per-project sessions volume | Read-write |
| `/home/opencode/.local/share/opencode/log/` | Link to per-project logs volume | Read-write |
| `/var/lib/opencode-sandbox/sessions/` | Sessions volume backing path | Read-write |
| `/var/lib/opencode-sandbox/logs/` | Logs volume backing path | Read-write |
| `/home/opencode/.local/` | User-level package installs | Read-write |
| `/tmp` | Per-run tmpfs | Read-write |

The `/workspace` mount uses `host-perms=mirror`. Files and directories created or chmod'd inside the sandbox keep their rwx bits on the host inode. Scripts stay executable. Git's exec-bit tracking stays consistent. Only ordinary rwx bits are mirrored. Ownership, file type, and setuid/setgid are not mirrored. An owner-access floor always applies.

Other exports keep the sandbox's default private metadata policy. Under this policy, guest-created files appear on the host as owner-only (`600`/`700`).

Project-secret sources (`~/.<project>/secrets.env` and `secrets.yaml`) are never mounted, copied, snapshotted, or built into the image. They stay host-only. Only the policy path reaches the runtime.

### Host Config and Isolated State

`run.sh` copies existing regular top-level host config files and directories into a temporary host-side snapshot. The snapshot excludes the credential file, opencode's generated install artifacts (`node_modules`, `package.json`, `package-lock.json`, `bun.lock`, `.gitignore`), and internal synchronization metadata. `run.sh` dereferences symlinks at every depth while the host paths are available. Linked agents, commands, plugins, skills, and other config become ordinary snapshot files and directories. A dangling link nested inside a snapshotted directory fails startup, so broken resources are never installed. A dangling top-level config entry is skipped.

Snapshot entries are mounted individually and read-only under `/etc/opencode-sandbox/bootstrap/opencode`. On every start, the entrypoint replaces host-managed project copies with the current host versions. The entrypoint also removes resources deleted from the host. Config created only inside the sandbox remains persistent. Sandbox edits to host-managed config are writable during a run. The next start replaces them from the host.

The separate source and destination layout keeps opencode's config writes on one writable filesystem. opencode writes seeding defaults, plugin dependency installs, and other files into its config dir during a run. A file bind mount is itself a mount point. Writes through it can fail with `EBUSY` when a process replaces a file. The project-local copy avoids that.

`auth.json` is a plain file in the persistent home volume. opencode owns the file: format, writes, and refresh behavior. The host file is never mounted.

#### Logging in

The guest cannot open a browser or receive the OAuth redirect. Run the host helper once per project:

```bash
opencode-login-openai ~/Projects/my-project
```

The helper opens the OpenAI authorization flow in your host browser. On a headless host, or when the callback port is busy, it prints the authorization URL and accepts a pasted redirect URL. The helper writes `/home/opencode/.local/share/opencode/auth.json` inside the project volume in opencode's own document format. Every sandbox for that project loads the same credential. `install.sh` links the helper to `~/.local/bin`. One login per project is enough; the credential persists until `./run.sh --reset` removes the volume.

opencode refreshes OAuth tokens itself and has no cross-process refresh lock. Per-project credential files keep different projects from racing each other. Two sandboxes for one project share that project's file and follow opencode's own refresh behavior, so avoid running two sessions of one project at the moment of a token refresh.

Microsandbox creates nested mount targets as root before launching the configured user. The launcher mounts the isolated storage/log volumes at backing paths outside `/home/opencode`. A new persistent data directory stays owned by UID 1000. The entrypoint creates links at opencode's normal data paths. Mount setup cannot create an unwritable data directory before startup synchronization. Existing non-empty storage or log directories are merged into the named volumes. The merge does not overwrite canonical volume data.

Host session history is never mounted. The per-project volumes replace it.

## Security Model

| Threat | Mitigation |
| --- | --- |
| Agent reads other projects | Only the current directory is mounted as `/workspace` |
| Agent modifies host opencode config | Config is exposed through read-only dereferenced snapshots. Writable config is project-local. `auth.json` is project-local too; login runs on the host via `opencode-login-openai`. |
| Agent modifies host history | Session storage and diagnostic logs use isolated per-project state |
| Agent escapes to host filesystem | Hardware-isolated microVM. Mounts are brokered host-side with path containment and identity virtualization. |
| Agent escalates to root in guest | Runs as unprivileged `opencode` (UID 1000). Uses the `restricted` profile. Has no setuid/setgid image binaries. |
| Agent modifies the image rootfs | Root writes are permission-limited. The writable overlay is discarded after every run. `/tmp` is a separate tmpfs. |
| Network exfiltration | Public internet, gateway DNS, and hosts listed in `OPENCODE_SANDBOX_LAN_HOSTS` are allowed for egress. Other private addresses and all unpublished inbound traffic are denied. |
| Persistent volume as attack vector | The volume is microsandbox-managed, not a host bind mount. Intra-project persistence of malicious files is possible but contained. |
| Secrets leak through images | Credentials are never baked into the image. Protected project secrets reach the guest only as runtime placeholders. |
| Agent reads protected API keys | Project secrets appear in the guest only as `$MSB_<NAME>` placeholders. The runtime substitutes real values only for the policy-allowed destinations and request locations of each secret. |

## Reset

```bash
./run.sh --reset
```

This removes the project's home, session, and log volumes. Installed tools, isolated history, custom `.bashrc` edits, the project credential, and other per-project state are removed. Host `~/.config/opencode` remains.

`--reset` bypasses secret discovery entirely. It removes the volumes without reading or validating the project-secret sources. Invalid secrets never block a reset.

## Testing

```bash
uv run pytest tests/
```

The test suite covers:

- **Unit tests** — script existence and syntax, Containerfile directives, Makefile targets, config files, run.sh flag generation, package-approval flow.
- **Integration tests** — image build/load, filesystem layout, host-config synchronization, project-local credential writes, isolated sessions/logs, persistence, and volume isolation.
- **Security tests** — security flags, mount allowlist, dangerous-character rejection.
- **Project-secret tests** — exact-directory mapping, paired-source sanity checks and reserved names, placeholder injection, and forwarding precedence.

Integration tests build the image once per session. They require `msb` and podman. The tests are skipped automatically when either tool is not available.

## Requirements

- [microsandbox](https://docs.microsandbox.dev/quickstart) CLI. Linux needs KVM. macOS needs Apple Silicon. Protected project secrets need an `msb` runtime that supports `run --secret-conf`. This applies only when a `secrets.env`/`secrets.yaml` pair is present.
- podman. It is used only to build the OCI image.
- Bash 4+

## See Also

- [docs/SPEC.md](docs/SPEC.md) — Behavioral specification
- [opencode](https://github.com/sst/opencode) — the coding agent
- [microsandbox](https://github.com/superradcompany/microsandbox) — microVM runtime
- [tau-sandbox](https://github.com/naripok/tau-sandbox) — the tau launcher this project forks
