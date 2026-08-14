# Agent Environment Reference

> This document is injected into your system prompt so you know what your sandbox can and cannot do. Treat it as reference material to know about constraints or capabilities inside the sandbox environment.

You are running inside a **hardware-isolated microsandbox microVM** (Arch Linux). The VM has its own kernel and networking; the host is only reachable through the explicit mounts below.

## Filesystem

| Path                  | Access     | Description                                                     |
| --------------------- | ---------- | --------------------------------------------------------------- |
| `/workspace`          | Read-write | Project directory (bind-mounted from host). Your working dir.   |
| `/home/tau`           | Read-write | Persistent named volume — survives across runs. Tools, shell.   |
| `/home/tau/.local/`   | Read-write | User-level package installs (`pip --user`, `uv`, `npm -g`).     |
| `/home/tau/.tau`      | Read-write | Host's `~/.tau/` — shared config: login tokens, skills, prompts, themes, sessions, logs (mounted when present). |
| `/home/tau/.agents`   | Read-write | Host's `~/.agents/` — shared global skills and prompts (mounted when present). |
| `/` (rootfs)          | Ephemeral  | Fresh from the image on every run. **Discarded when the run ends.** |

**Key rule:** the microVM root filesystem does **not** persist. Anything installed or changed outside `/workspace` and `/home/tau` is lost when the sandbox exits. System-package installs (`pacman`) are not available anyway — you run without root.

**Not accessible:** other project directories, the rest of the host home (except the shared `~/.tau` and `~/.agents` mounts above), host SSH keys, host dotfiles, host sockets, and any host filesystem outside the mounts above.

## Identity & Security

- Runs as `tau` (UID 1000). No root, no sudo, no package-manager root access.
- The VM is hardware-isolated: even a fully compromised guest cannot modify the host kernel or host files outside the mounts.
- All mounts are host-side enforced; the config mounts are deliberately read-write so the agent can use the host's Tau login.
- Bind mounts are identity-virtualized: files you create in `/workspace` or `/home/tau` (including the shared `~/.tau` and `~/.agents`) appear on the host under the host user's identity.
- npm lifecycle scripts are disabled by default (`ignore-scripts=true`); opt in per-command with `--ignore-scripts=false`.

## Installed Tools

**Languages:** Python, pip, uv, Node.js, npm
**System:** bash, git, gcc, make, rsync, fd, ripgrep, ast-grep, openssh, curl, tar
**Agent:** tau (Tau coding agent)

**Package installs** persist in the volume: `pip install --user` → `~/.local/`, `uv tool install` → `~/.local/`, `npm install -g` → `~/.local/`.

## Per-Project System Dependencies

If the project needs system-level packages not available via pip/uv/npm:

1. Create `.tau-packages` in the project root (one package per line, `#` for comments) — Arch Linux package names.
2. The user must approve the packages on their next sandbox session; on approval the image rebuilds with them.
3. If you need to update `.tau-packages`, tell the user: "I've updated `.tau-packages`. Re-enter the sandbox to approve and rebuild."

## Network

- Outbound internet access is allowed (HTTPS, git, package registries, model APIs). DNS works.
- Inbound is blocked — nothing can connect to this VM from the host or the network.
- Host services are NOT reachable via `localhost` (separate network stack).
- Env vars from the host `~/.env` are forwarded (API keys for model providers, etc.). Run `env` to see what is available.

## Resources

4 vCPU cores, 8 GB memory, 1024 processes per sandbox. Override with `TAU_CPUS`, `TAU_MEM`, `TAU_PIDS` on the host.

## Persistence

- `/workspace` and `/home/tau` persist across runs; everything else is ephemeral.
- `/home/tau/.tau` and `/home/tau/.agents` are the host's own config directories: sessions, logs, credentials, trust data, and lock files are the same inside the sandbox and on the host, and Tau login tokens from the host are usable here.
- `APPEND_SYSTEM.md` (this file) is refreshed on every start from the sandbox repo or image.

## Shell

- Bash. `PATH` includes `~/.local/bin`. `PYTHONUSERBASE`, `NPM_CONFIG_PREFIX`, and `PIP_USER` point user-level installs at `~/.local`.

## Troubleshooting

| Problem                                      | Solution                                                            |
| -------------------------------------------- | ------------------------------------------------------------------- |
| Command not found                            | `pip install --user` / `uv tool install` / `npm install -g`         |
| Cannot install a system package (no root)    | Add it to `.tau-packages` and ask the user to approve a rebuild     |
| A change under `/usr`, `/etc` vanished       | Rootfs is ephemeral — those locations reset on every run            |
| npm fails on native modules                  | `npm install --ignore-scripts=false`                                |
| Cannot reach a host service on localhost     | Separate network stack — host services aren't reachable             |

## Agent Behavior

Now, continue with your regular agent behavior as instructed in your system prompt, keeping in mind the limitations and capabilities of this sandbox.
