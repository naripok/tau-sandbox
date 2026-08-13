# Tau Agent Isolation Environment

Per-project isolation for the [Tau coding agent](https://github.com/huggingface/tau) using [microsandbox](https://github.com/superradcompany/microsandbox) microVMs.

Each project runs in its own hardware-isolated microVM with a persistent volume. Sessions, installed tools, and shell customizations survive across runs. Projects remain isolated from each other and from the host.

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
tau-sandbox --reset                         # wipe persistent volume
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
~/Projects/my-project/   ───────► /workspace        (read-write virtiofs mount)
~/.tau/                  ───────► /tau-source       (read-only)
~/.agents/               ───────► /agents-source    (read-only, optional)
                          ───────► /home/tau        (persistent named volume)

podman volume (source of image) → msb run → boot microVM → entrypoint → tau
```

- **One project, one sandbox, one volume.** Each project gets its own persistent named volume `tau-persist-<project>-<hash>`.
- **Ephemeral microVM.** The VM boots fresh from the image on every run and is discarded afterwards. Everything durable lives in `/workspace` (bind mount) and `/home/tau` (volume).
- **Hardware isolation.** The guest has its own kernel; the host is reachable only through the explicit mounts, all enforced host-side.
- **Read-only host config.** The agent can use global Tau skills and settings but cannot modify them. Host config is rsynced into the volume on every start.
- **Persistent state.** Sessions (`~/.tau/sessions`), tool installs (`~/.local`), and shell customizations survive across runs.
- **Transparent pair-coding.** Because the project directory is a bind mount, your host editor and the sandbox agent see the same files simultaneously — no sync step.

## Architecture

| Component                 | Description                                                           |
| ------------------------- | --------------------------------------------------------------------- |
| `Containerfile`           | Arch Linux image with Python, uv, Node.js, tau, and the entrypoint    |
| `config/entrypoint.sh`    | Syncs host config, sets up the volume and shell, drops into the command |
| `config/.bashrc`          | Shell prompt, aliases, and persistent PATH configuration              |
| `config/APPEND_SYSTEM.md` | Agent environment reference — auto-injected into the system prompt    |
| `run.sh`                  | Launch script — ensures the image, mounts project + volumes, boots VM |
| `install.sh`              | Prerequisite checks, image build/load, and alias setup                |
| `Makefile`                | Convenience targets (`build`, `shell`, `tau`, `clean`, `reset`)       |
| `tests/`                  | Pytest suite covering build, filesystem, persistence, and integration |

## Agent Environment Awareness

The sandboxed agent knows it is in a microVM — and exactly what it can and cannot do. This is not guessed or inferred; it is explicitly told via system prompt injection.

The entrypoint copies `config/APPEND_SYSTEM.md` into the volume's `~/.tau/APPEND_SYSTEM.md` on every start, and Tau automatically injects that file into the system prompt. Every agent session receives a complete description of the sandbox: filesystem layout, installed tools, security model, network, resource limits, persistence behavior, and troubleshooting tips.

The file is committed in the repository (and baked into the image at `/etc/tau-sandbox/APPEND_SYSTEM.md`), and overwritten on every start, so it stays in sync with the actual sandbox configuration. When the Containerfile adds a new tool or `run.sh` changes a flag, `APPEND_SYSTEM.md` is updated to match.

## Configuration

All settings are controlled via environment variables:

| Variable           | Default             | Description                                                        |
| ------------------ | ------------------- | ------------------------------------------------------------------ |
| `TAU_IMAGE`        | `tau-agent-isolated`| Full image reference used by msb; bypasses `.tau-packages` and automatic build/load |
| `TAU_CONFIG_DIR`   | `~/.tau`            | Host Tau config, mounted read-only at `/tau-source`                 |
| `TAU_AGENTS_DIR`   | `~/.agents`         | Host `.agents` config, mounted read-only at `/agents-source`        |
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
| `TAU_NO_UPDATE_CHECK`| Skips Tau's online version check (the image is the update vehicle) |

### Sandbox Filesystem

| Path                                | Source                      | Permissions |
| ----------------------------------- | --------------------------- | ----------- |
| `/workspace`                        | Current directory           | Read-write  |
| `/tau-source`                       | `~/.tau/` (if it exists)    | Read-only   |
| `/agents-source`                    | `~/.agents/` (if it exists) | Read-only   |
| `/home/tau`                         | Persistent named volume     | Read-write  |
| `/home/tau/.tau/`                   | Synced from `/tau-source/`  | Read-write  |
| `/home/tau/.tau/sessions/`          | Session history             | Read-write  |
| `/home/tau/.local/`                 | User-level package installs | Read-write  |

### Config Sync

On every sandbox start, the entrypoint syncs host config into the persistent volume:

| Synced from host                     | Preserved in volume          |
| ------------------------------------ | ---------------------------- |
| New skills in `~/.tau/skills/`       | Sessions in `sessions/`      |
| New/changed `catalog.toml`           | Logs in `logs/`              |
| Updated prompts/themes               | `credentials.json`, `trust.json`, lock files |

Files deleted from the host are **not** removed from the volume (to avoid accidentally deleting user data). Use `./run.sh --reset` for a clean slate.

## Security Model

| Threat                             | Mitigation                                                                                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent reads other projects         | Only the current directory is mounted as `/workspace`                                                                                       |
| Agent modifies host config         | Mounted `:ro` at `/tau-source` / `/agents-source` — enforced host-side, writable copies go to the volume only                                |
| Agent escapes to host filesystem   | Hardware-isolated microVM (own kernel). Bind mounts are brokered host-side with path containment and identity virtualization                  |
| Agent escalates to root in guest   | Runs as unprivileged user `tau` (1000) with the `restricted` security profile (`no_new_privs`, capability drops)                             |
| Network exfiltration               | Outbound allowed (needed for model APIs and package installs); inbound denied via explicit ingress policy                                    |
| Persistent volume as attack vector | Volume is microsandbox-managed, not a host bind mount. Intra-project persistence of malicious files is possible but contained                |
| Secrets leak through images        | API keys are forwarded per-run from host env; never baked into the image                                                                    |

## Reset

```bash
./run.sh --reset
```

This removes the project's persistent volume. **All persistent data is destroyed**: sessions, installed tools, custom `.bashrc` edits, and any other state. The next run re-initializes from current host config.

## Testing

```bash
pytest tests/
```

The test suite covers:

- **Unit tests** — script existence and syntax, Containerfile directives, Makefile targets, config files, run.sh flag generation, package-approval flow
- **Integration tests** — image build/load, filesystem layout, mount correctness, config sync, persistence across runs, volume isolation
- **Security tests** — security flags, read-only config mounts, dangerous-character rejection

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
