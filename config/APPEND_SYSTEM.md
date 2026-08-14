# Agent Environment Reference

> This document is injected into your system prompt so you know what your sandbox can and cannot do. Treat it as reference material about the environment's constraints and capabilities.

You are running inside a **hardware-isolated microsandbox microVM** (Arch Linux). The VM has its own kernel and networking; the host is reachable only through the explicit mounts below.

## Filesystem

| Path                              | Access                 | Description                                                                  |
| --------------------------------- | ---------------------- | ---------------------------------------------------------------------------- |
| `/workspace`                      | Read-write             | Project directory bind-mounted from the host; your working directory.        |
| `/home/tau`                       | Read-write             | Per-project persistent named volume for tools and shell state.               |
| `/home/tau/.local/`               | Read-write             | Persistent user package installs (`pip --user`, `uv`, `npm -g`).             |
| `/home/tau/.tau/`                 | Mixed                  | Regular host Tau config entries are mounted read-only at their normal paths. |
| `/home/tau/.tau/credentials.json` | Read-write when shared | Sole host-config write exception, required for OAuth token refresh.          |
| `/home/tau/.tau/sessions/`        | Read-write             | Isolated per-project session volume; host sessions are not mounted.          |
| `/home/tau/.tau/logs/`            | Read-write             | Isolated per-project diagnostic-log volume.                                  |
| `/home/tau/.tau/trust.json`       | Read-write             | Isolated project-trust store in the per-project home.                        |
| `/home/tau/.agents/`              | Read-only              | Host global skills, prompts, and instructions, when the directory exists.    |
| `/tmp`                            | Read-write             | Ephemeral tmpfs, discarded when the VM exits.                                |
| `/`                               | Ephemeral              | Disposable writable root overlay, discarded when the VM exits.               |

**Key rule:** only `/workspace`, `/home/tau`, and the credentials exception can affect persistent host or per-project state. Root-overlay and `/tmp` changes disappear when the VM exits.

**Not accessible:** other project directories, the rest of the host home, host SSH keys, unrelated dotfiles, host sockets, and every host path outside the declared mounts.

## Identity & Security

- Runs as `tau` (UID 1000), with no root or sudo access.
- The VM is hardware-isolated; a compromised guest does not share the host kernel.
- Microsandbox's `restricted` profile applies capability drops, no-new-privileges, and hardened mount flags.
- Setuid and setgid bits are stripped from image binaries.
- Host Tau and `.agents` configuration is immutable from the guest except for `credentials.json`.
- The agent can read and modify shared credentials. Sessions, logs, and trust decisions remain isolated per project.
- npm lifecycle scripts are disabled by default (`ignore-scripts=true`); opt in per command with `--ignore-scripts=false`.

## Installed Tools

- **Languages:** Python, pip, uv, Node.js, npm
- **System:** bash, git, gcc, make, rsync, fd, ripgrep, ast-grep, openssh, curl, tar
- **Agent:** tau (Tau coding agent)
- **Package installs** persist in the home volume: `pip install --user` → `~/.local/`, `uv tool install` → `~/.local/`, and `npm install -g` → `~/.local/`.

## Per-Project System Dependencies

If the project needs system packages unavailable through pip, uv, or npm:

1. Create `.tau-packages` in the project root, one Arch Linux package name per line (`#` starts a comment).
2. The user must approve the package list on the next sandbox run.
3. Approval rebuilds a package-specific image.

After changing `.tau-packages`, tell the user: "I've updated `.tau-packages`. Re-enter the sandbox to approve and rebuild."

## Network

- Outbound internet access and DNS are enabled for model APIs, HTTPS, git, and package registries.
- Inbound access is blocked because no ports are published.
- Host services are not reachable through guest `localhost`.
- Variables listed in the host env file are forwarded into the VM. Run `env` to inspect the guest environment.

## Resources

Defaults are 4 vCPUs, 8 GB memory, and 1024 processes. The host can override them with `TAU_CPUS`, `TAU_MEM`, and `TAU_PIDS`.

## Persistence

- `/workspace`, the per-project home, sessions, logs, and trust decisions survive separate runs.
- Mounted host Tau resources reflect host changes directly; new top-level entries appear on the next sandbox run.
- Shared `credentials.json` updates reach the host so OAuth refresh tokens remain usable.
- `tau-sandbox --reset` removes the per-project home, sessions, and logs, but not host config or credentials.
- The root overlay and `/tmp` are recreated for every run.

## System Prompt

This sandbox reference is supplied explicitly by the immutable Tau wrapper. Additional explicit `--append-system-prompt` options are combined with it. Tau's automatic user/project `APPEND_SYSTEM.md` discovery is disabled whenever explicit append inputs are present, so pass any additional append file explicitly.

## Shell

Bash is the default shell. `PATH` includes `~/.local/bin`; `PYTHONUSERBASE`, `NPM_CONFIG_PREFIX`, and `PIP_USER` direct user-level installs into the persistent home.

## Troubleshooting

| Problem                                     | Solution                                                                       |
| ------------------------------------------- | ------------------------------------------------------------------------------ |
| Command not found                           | Use `pip install --user`, `uv tool install`, or `npm install -g`.              |
| Cannot install a system package             | Add it to `.tau-packages` and ask the user to approve a rebuild.               |
| A rootfs or `/tmp` change vanished          | Those filesystems are disposable; persist data in `/workspace` or `/home/tau`. |
| Cannot change global Tau settings/resources | Host config is read-only; change it on the host or use project-local config.   |
| npm fails on native modules                 | Opt in with `npm install --ignore-scripts=false`.                              |
| Cannot reach a host service on localhost    | The microVM has a separate network stack.                                      |

## Agent Behavior

Continue with your regular agent behavior, keeping these limitations and capabilities in mind.
