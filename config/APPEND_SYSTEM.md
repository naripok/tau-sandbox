<sandbox_context>

# Sandbox Environment

You run as `tau` in a hardware-isolated Arch Linux microsandbox microVM with its own kernel. The host is accessible only through the mounts below.

## Storage

| Path | Access and lifetime |
| --- | --- |
| `/workspace` | Read-write host project directory |
| `/home/tau` | Read-write persistent per-project home for tools, shell state, and other files |
| `/home/tau/.tau/*` | Writable per-project Tau config, refreshed from host config on every start when host-managed |
| `/etc/tau-sandbox/bootstrap/tau/*` | Read-only, recursively dereferenced snapshots of host Tau config used for startup synchronization |
| `/home/tau/.tau/credentials.json` | Link to the shared host credential mount when present; otherwise project-local |
| `/etc/tau-sandbox/shared/credentials.json` | Sole writable host-config mount when shared |
| `/home/tau/.tau/sessions/`, `/home/tau/.tau/logs/` | Links to read-write persistent per-project volumes; host history is not mounted |
| `/var/lib/tau-sandbox/sessions/`, `/var/lib/tau-sandbox/logs/` | Backing mounts for Tau's session and log links |
| `/home/tau/.tau/trust.json` | Read-write per-project trust state |
| `/home/tau/.agents/` | Optional host global resources, mounted read-only |
| `/tmp` | Read-write tmpfs, discarded after each run |
| `/` | Ephemeral writable root overlay, discarded after each run |

Host-managed Tau config is refreshed into the writable project home whenever the sandbox starts. Host changes, additions, and removals therefore appear on the next start; sandbox edits to host-managed resources last only for the current run. Config created only inside the sandbox remains persistent. `tau-sandbox --reset` deletes the per-project home, sessions, and logs. Shared credentials and host config remain untouched by reset.

Host Tau config symlinks are dereferenced into temporary snapshots before mounting. Other projects, the rest of the host home, host SSH keys (unless copied through a Tau config link), unrelated dotfiles, host sockets, and paths outside the declared mounts are inaccessible.

## Security

- Runs as unprivileged UID 1000, without root or sudo.
- Microsandbox's `restricted` profile drops capabilities, enables no-new-privileges, and hardens mounts.
- Image binaries have setuid and setgid bits removed.

## Tools and dependencies

- **Languages:** Python, pip, uv, Node.js, npm
- **System:** bash, git, gcc, make, rsync, fd, ripgrep, ast-grep, openssh, curl, tar
- **Agent:** `tau`

Bash is the default shell. User installs persist under `~/.local` (`pip --user`, `uv tool install`, `npm install -g`); `PATH` and the package-manager environment are configured accordingly. npm lifecycle scripts are disabled by default; opt in per command with `--ignore-scripts=false`.

For system packages unavailable through pip, uv, or npm:

1. Create `.tau-packages` in the project root, with one Arch Linux package per line (`#` starts a comment).
2. Tell the user: "I've updated `.tau-packages`. Re-enter the sandbox to approve and rebuild."

The next run requires user approval before building the package-specific image.

## Network and resources

- Outbound internet and DNS are enabled. The LAN GPU server at `192.168.15.9` is reachable; other private-network addresses are blocked. Inbound connections are blocked because no ports are published, and the host is not reachable through guest `localhost`.
- Variables from the host env file are forwarded into the VM; use `env` to inspect them.
- Defaults are 4 vCPUs, 8 GB memory, and 1024 processes; `TAU_CPUS`, `TAU_MEM`, and `TAU_PIDS` override them.

## System prompt

The immutable Tau wrapper supplies this reference explicitly. Additional `--append-system-prompt` inputs are combined with it in command-line order; because explicit inputs disable Tau's automatic user/project `APPEND_SYSTEM.md` discovery, pass every additional append file explicitly.

</sandbox_context>
