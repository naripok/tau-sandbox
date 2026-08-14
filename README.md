# Tau Agent Isolation Environment

Per-project isolation for the [Tau coding agent](https://github.com/huggingface/tau) using [microsandbox](https://github.com/superradcompany/microsandbox) microVMs.

Each project runs in its own hardware-isolated microVM with persistent home, session, and log volumes. Installed tools and shell customizations survive across runs, while Tau sessions and diagnostics remain isolated per project. Host Tau resources are exposed read-only, with `credentials.json` as the sole write exception for OAuth refresh.

## The Problem

AI coding agents execute arbitrary shell commands, read files, and install packages. Running them directly on the host means:

- An agent on `project-a` can read secrets from `project-b`
- A compromised npm package can access SSH keys, dotfiles, and every project on the machine
- Sessions, installed tools, and settings are lost every time the sandbox exits

This project solves all three problems with a microVM-per-project model backed by persistent named volumes. Unlike container-based sandboxes (which share the host kernel), every Tau session here runs inside a hardware-isolated microVM with its own kernel and network stack.

## Quick Start

```bash
git clone https://github.com/naripok/tau-sandbox.git ~/tau-sandbox
cd ~/tau-sandbox && ./install.sh
```

Requires [microsandbox](https://docs.microsandbox.dev/quickstart) (`msb`) and podman (only for image builds).

Add the printed alias to your `~/.bashrc` or `~/.zshrc`, then use it from any project:

```bash
cd ~/Projects/my-project
tau-sandbox tau -p "Review this codebase"   # run tau in one-shot mode
tau-sandbox tau                             # start the Tau TUI
tau-sandbox bash                            # interactive shell
tau-sandbox npm test                        # any command inside
tau-sandbox --reset                         # wipe per-project persistent state
```

The first run builds the Arch Linux image and loads it into the microsandbox cache. Subsequent runs boot in well under a second.

## Per-Project System Dependencies

The sandbox image ships with a fixed set of tools (Python, uv, Node.js, git, gcc, etc.). If your project needs additional system-level packages (CMake, libffi, ffmpeg), declare them in a `.tau-packages` file in the project root:

```
# Build tools
cmake
pkgconf

# Cryptography
libffi
```

**How it works:**

1. Create `.tau-packages` in your project root (one Arch package per line, `#` for comments).
2. On the next `tau-sandbox` run, the script detects the file and prompts you to approve the packages.
3. On approval, a per-project image is built with those packages installed and loaded into the microsandbox cache.
4. The image name includes a hash of `.tau-packages`, so changes trigger a new approval.

Projects without `.tau-packages` use the shared base image — zero overhead.

**Security:** Every change to `.tau-packages` requires explicit user approval before the image is rebuilt. The agent can write `.tau-packages` but cannot bypass the approval gate. Non-interactive mode (pipes, CI) refuses to rebuild without approval.

**Override:** Set `TAU_IMAGE=my-image-ref` to bypass `.tau-packages` and automatic image management entirely and use a specific image (load it yourself, e.g. `make build`).

## How It Works

```
Host                              Sandbox (microVM)
─────────────────                 ─────────────────
~/Projects/my-project/   ───────► /workspace                  (read-write)
~/.tau/*                 ───────► /home/tau/.tau/*           (read-only entries)
~/.tau/credentials.json  ───────► ~/.tau/credentials.json    (read-write exception)
~/.agents/               ───────► /home/tau/.agents          (read-only, optional)
msb home volume          ───────► /home/tau                  (read-write)
msb sessions volume      ───────► ~/.tau/sessions            (read-write, isolated)
msb logs volume          ───────► ~/.tau/logs                (read-write, isolated)
(home volume state)      ───────► ~/.tau/trust.json          (read-write, isolated)

podman image → msb run → boot microVM → entrypoint → tau wrapper → Tau
```

- **One project, one sandbox, isolated state.** Each project gets separate home, session, and diagnostic-log volumes keyed by its resolved path.
- **Ephemeral microVM.** The VM boots with a disposable writable root overlay and explicit `/tmp` tmpfs; both are discarded afterwards. Microsandbox has no container-style `--read-only` rootfs switch, so the image instead runs unprivileged, strips setuid/setgid bits, and relies on the disposable overlay.
- **Hardware isolation.** The guest has its own kernel; the host is reachable only through explicit mounts enforced host-side.
- **Read-only host resources.** Existing top-level `~/.tau` entries and `~/.agents` are mounted read-only at Tau's normal paths, excluding isolated state. `credentials.json` alone is writable so rotated OAuth tokens remain valid.
- **Isolated sessions, logs, and trust.** Tau history, diagnostics, and project trust decisions persist per project rather than modifying host state.
- **Transparent pair-coding.** Because the project directory is a bind mount, your host editor and the sandbox agent see the same files simultaneously.

## Architecture

| Component                 | Description                                                           |
| ------------------------- | --------------------------------------------------------------------- |
| `Containerfile`           | Arch Linux image with Python, uv, Node.js, tau, and the entrypoint    |
| `config/entrypoint.sh`    | Initializes the persistent home and sandbox environment               |
| `config/tau-wrapper.py`   | Injects invariant sandbox context and handles mounted credential writes |
| `config/.bashrc`          | Shell prompt, aliases, and persistent PATH configuration              |
| `config/APPEND_SYSTEM.md` | Immutable agent environment reference                                 |
| `run.sh`                  | Launch script — ensures the image, mounts project + state, boots VM   |
| `install.sh`              | Prerequisite checks, image build/load, and alias setup                |
| `Makefile`                | Convenience targets (`build`, `shell`, `tau`, `clean`, `reset`)       |
| `tests/`                  | Pytest suite covering build, filesystem, persistence, and integration |

## Agent Environment Awareness

The sandboxed agent knows it is in a microVM — and exactly what it can and cannot do. This is not guessed or inferred; it is explicitly told via system prompt injection.

`run.sh` mounts `config/APPEND_SYSTEM.md` read-only at `/etc/tau-sandbox/APPEND_SYSTEM.md`. The image's `tau` wrapper always prepends `--append-system-prompt` with that path, so project or user prompt files cannot shadow the sandbox reference and host config is never overwritten.

Additional explicit `--append-system-prompt` options are combined in command-line order. Because Tau treats any explicit append input as higher precedence, automatic user/project `APPEND_SYSTEM.md` discovery is suppressed; pass another append file explicitly when it is needed. The image also contains a fallback copy for direct image use.

## Configuration

All settings are controlled via environment variables:

| Variable           | Default             | Description                                                        |
| ------------------ | ------------------- | ------------------------------------------------------------------ |
| `TAU_IMAGE`        | `tau-agent-isolated`| Full image reference used by msb; bypasses `.tau-packages` and automatic build/load |
| `TAU_CONFIG_DIR`   | `~/.tau`            | Host Tau config source; entries are read-only except `credentials.json` |
| `TAU_AGENTS_DIR`   | `~/.agents`         | Host `.agents` resources, mounted read-only at `/home/tau/.agents` |
| `TAU_ENV_FILE`     | `~/.env`            | Env file whose variables are forwarded into the sandbox             |
| `TAU_CPUS`         | `4`                 | Virtual CPUs for the sandbox                                        |
| `TAU_MEM`          | `8G`                | Memory for the sandbox                                              |
| `TAU_PIDS`         | `1024`              | Process (nproc) limit inside the sandbox                            |

### Environment Variables

Variables defined in `~/.env` (or the path set by `TAU_ENV_FILE`) are automatically forwarded into the sandbox. This is how you pass API keys (`VLLM_API_KEY`, `OPENROUTER_API_KEY`, etc.) without baking them into the image.

Example `~/.env`:

```
OPENROUTER_API_KEY=sk-or-...
VLLM_API_KEY=...
```

### Sandbox Environment Variables

In addition to forwarded host variables, the entrypoint sets sandbox-specific defaults on every boot:

| Variable             | Description                                             |
| -------------------- | ------------------------------------------------------- |
| `TAU_NO_UPDATE_CHECK` | Skips Tau's online version check (the image is the update vehicle) |
| `TAU_SANDBOX_SHARED_CREDENTIALS` | Internal `run.sh` marker selecting bind-mount-safe credential writes |

### Sandbox Filesystem

| Path                                | Source                           | Permissions |
| ----------------------------------- | -------------------------------- | ----------- |
| `/workspace`                        | Current directory                | Read-write  |
| `/home/tau/.tau/*`                  | Existing host `~/.tau` entries   | Read-only   |
| `/home/tau/.tau/credentials.json`   | Host login tokens, when present  | Read-write  |
| `/home/tau/.tau/sessions/`          | Per-project sessions volume      | Read-write  |
| `/home/tau/.tau/logs/`              | Per-project logs volume          | Read-write  |
| `/home/tau/.tau/trust.json`         | Per-project home volume          | Read-write  |
| `/home/tau/.agents/`                | Host `~/.agents/` (if present)   | Read-only   |
| `/home/tau`                         | Per-project home volume          | Read-write  |
| `/home/tau/.local/`                 | User-level package installs      | Read-write  |
| `/tmp`                              | Per-run tmpfs                    | Read-write  |

### Host Config and Isolated State

`run.sh` mounts existing regular top-level host `~/.tau` files and directories read-only at the corresponding sandbox paths, excluding credentials, sessions, logs, trust-store files, and symlinks. This keeps settings, providers, prompts, skills, themes, extensions, and other resources visible without exposing the host config directory to writes. New top-level entries appear on the next sandbox run.

`credentials.json` is mounted read-write as a deliberate exception. Tau normally updates credentials by atomically replacing the file, which file mounts cannot support, so the sandbox wrapper uses a bind-mount-safe in-place writer only when shared credentials are mounted. If the host credential file is absent, Tau can create project-local credentials in the persistent home instead.

Host sessions and logs are excluded and replaced by per-project named volumes. Trust-store files live in the per-project home because Tau needs a writable lock and atomic updates, and host trust paths would not match the guest's canonical `/workspace` path. `~/.agents` is mounted read-only. Global settings changes must be made on the host; project-local `.tau` and `.agents` configuration remains writable through `/workspace`.

## Security Model

| Threat                             | Mitigation                                                                                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent reads other projects         | Only the current directory is mounted as `/workspace`                                                                                       |
| Agent modifies host Tau config     | Existing Tau and `.agents` resources are read-only; only `credentials.json` is writable for login and OAuth refresh                         |
| Agent modifies host history/trust  | Sessions, diagnostic logs, and trust decisions use isolated per-project state                                                                |
| Agent escapes to host filesystem   | Hardware-isolated microVM; mounts are brokered host-side with path containment and identity virtualization                                  |
| Agent escalates to root in guest   | Runs as unprivileged `tau` (1000), uses the `restricted` profile, and has no setuid/setgid image binaries                                    |
| Agent modifies the image rootfs    | Root writes are permission-limited and the writable overlay is discarded after every run; `/tmp` is a separate tmpfs                        |
| Network exfiltration               | Outbound + gateway DNS allowed via the `public` network profile; inbound denied (no published ports)                                                  |
| Persistent volume as attack vector | Volume is microsandbox-managed, not a host bind mount. Intra-project persistence of malicious files is possible but contained                |
| Secrets leak through images        | API keys are forwarded per-run from host env; never baked into the image                                                                    |

## Reset

```bash
./run.sh --reset
```

This removes the project's home, session, and log volumes: installed tools, isolated history, custom `.bashrc` edits, and other per-project state. Host `~/.tau`, `~/.agents`, and credentials remain.

## Testing

```bash
pytest tests/
```

The test suite covers:

- **Unit tests** — script existence and syntax, Containerfile directives, Makefile targets, config files, run.sh flag generation, package-approval flow
- **Integration tests** — image build/load, filesystem layout, read-only host resources, credential writes, isolated sessions/logs, persistence, and volume isolation
- **Security tests** — security flags, mount allowlist, dangerous-character rejection

Integration tests build the image once per session and require `msb` and podman. Tests are automatically skipped when either is not available.

## Requirements

- [microsandbox](https://docs.microsandbox.dev/quickstart) CLI (Linux needs KVM; macOS needs Apple Silicon)
- podman (used only to build the OCI image)
- Bash 4+

## See Also

- [docs/SPEC.md](docs/SPEC.md) — Behavioral specification
- [microsandbox](https://github.com/superradcompany/microsandbox) — microVM runtime
- [Tau](https://github.com/huggingface/tau) — the coding agent
- [pi-sandbox](https://github.com/naripok/pi-sandbox) — the container-based predecessor this project mirrors
