# Tau Sandbox Specification

## Purpose

Defines the behavioral requirements for the Tau coding agent sandbox: a per-project, hardware-isolated microVM built on microsandbox. The sandbox gives Tau a working coding environment while limiting host access to explicit mounts and isolating durable agent state by project.

## Requirements

### Requirement: Per-project persistent state

For a project at resolved path `<path>`, `run.sh` SHALL use the first eight hexadecimal characters of the SHA-256 of `<path>` (including the shell `echo` newline) and the project basename to derive:

- `tau-persist-<basename>-<hash>` mounted at `/home/tau`
- `tau-sessions-<basename>-<hash>` mounted at `/home/tau/.tau/sessions`
- `tau-logs-<basename>-<hash>` mounted at `/home/tau/.tau/logs`

#### Scenario: State survives separate runs

- GIVEN a run writes into the home, session, or log volume
- WHEN the same project starts another sandbox
- THEN the written state SHALL remain available

#### Scenario: Projects have distinct state

- GIVEN two different resolved project paths
- WHEN both launch sandboxes
- THEN all three derived volume names SHALL differ

### Requirement: Ephemeral microVM filesystems

Each invocation SHALL boot a fresh microVM and discard it when the command exits. The image root SHALL use microsandbox's disposable writable overlay. `/tmp` SHALL be an explicit tmpfs.

#### Scenario: Root and temporary writes disappear

- GIVEN one run writes a file outside persistent mounts or under `/tmp`
- WHEN a second run starts
- THEN the file SHALL NOT exist

### Requirement: Workspace bind mount

The current directory SHALL be mounted read-write at `/workspace` and selected as the guest working directory.

#### Scenario: Host and guest share project changes

- GIVEN a host project file
- WHEN the guest reads or modifies it under `/workspace`
- THEN both host and guest SHALL observe the same file

### Requirement: Read-only host Tau resources

When `TAU_CONFIG_DIR` exists, each regular top-level file or directory SHALL be mounted at `/home/tau/.tau/<name>` read-only, except credentials, sessions, logs, and trust-store files. Top-level symlinks and special files SHALL be ignored so they cannot expand the host-path allowlist:

- Host `sessions` and `logs` SHALL NOT be mounted.
- Host `trust.json`, `trust.json.lock`, and `trust.json.pending` SHALL NOT be mounted; trust state SHALL remain writable in the per-project home because Tau requires a writable lock and atomic replacement.
- The isolated named volumes SHALL occupy their normal Tau paths.
- When host `credentials.json` exists, it SHALL be mounted read-write at `/home/tau/.tau/credentials.json`.
- The host config directory itself SHALL NOT be mounted read-write.

This exposes current settings, providers, skills, prompts, themes, extensions, and other resources without permitting guest modification. A top-level host entry created after startup becomes available on the next run.

#### Scenario: Host resource is visible but immutable

- GIVEN host `~/.tau/settings.json` exists
- WHEN the sandbox starts
- THEN Tau SHALL read it from `/home/tau/.tau/settings.json`
- AND a guest write to that path SHALL fail

#### Scenario: Host history and trust are not exposed

- GIVEN host sessions, logs, and trust-store files contain data
- WHEN the sandbox starts
- THEN sessions and logs SHALL be shadowed by isolated volumes
- AND trust SHALL use writable state under the per-project home

### Requirement: Writable shared credentials exception

When host `credentials.json` exists, Tau SHALL be able to read and update that same host file so OAuth access and refresh token rotation remain valid outside the sandbox.

Microsandbox file mounts cannot be replaced atomically. The installed Tau wrapper SHALL therefore switch `FileCredentialStore` to a flushed, in-place write only when `TAU_SANDBOX_SHARED_CREDENTIALS=1`; all other credential stores SHALL retain Tau's normal atomic writer.

#### Scenario: Credential refresh reaches the host

- GIVEN host `credentials.json` is mounted
- WHEN Tau updates a stored credential
- THEN the host file SHALL contain the update

#### Scenario: Missing host credentials stay project-local

- GIVEN `TAU_CONFIG_DIR` exists without `credentials.json`
- WHEN Tau creates credentials
- THEN they SHALL be written into the per-project home
- AND the host config SHALL remain unchanged

### Requirement: Read-only `.agents` resources

When `TAU_AGENTS_DIR` exists, it SHALL be mounted read-only at `/home/tau/.agents`.

#### Scenario: Global skill is usable but immutable

- GIVEN a host `.agents` skill exists
- WHEN the sandbox starts
- THEN Tau SHALL be able to read it
- AND guest writes to the host skill SHALL fail

### Requirement: Invariant environment-reference injection

`run.sh` SHALL mount repository `config/APPEND_SYSTEM.md` read-only at `/etc/tau-sandbox/APPEND_SYSTEM.md`. The installed `/usr/local/bin/tau` wrapper SHALL prepend:

```text
--append-system-prompt /etc/tau-sandbox/APPEND_SYSTEM.md
```

before all caller arguments. The image SHALL also contain a fallback copy at that path.

Additional explicit append options SHALL remain in caller order and therefore combine with the sandbox reference. Tau's normal automatic `APPEND_SYSTEM.md` discovery is not cumulative with explicit startup input and SHALL be documented accordingly.

#### Scenario: Project prompt cannot shadow sandbox context

- GIVEN a project has `.tau/APPEND_SYSTEM.md`
- WHEN Tau starts normally through the wrapper
- THEN the explicit sandbox reference SHALL remain in the active system prompt

#### Scenario: Explicit additional prompt combines

- GIVEN the caller supplies another `--append-system-prompt`
- WHEN Tau starts
- THEN the sandbox reference SHALL precede the caller's append input

### Requirement: Hardened guest execution

The sandbox SHALL:

- run as user `tau` with UID/GID 1000
- use microsandbox's `restricted` security profile
- strip setuid and setgid bits from image files
- mount `/tmp` as tmpfs
- cap processes with `nproc`
- use the public network profile without publishing inbound ports

#### Scenario: Guest identity is unprivileged

- WHEN `id -u` runs in the guest
- THEN it SHALL print `1000`

#### Scenario: External DNS works while inbound remains closed

- WHEN the sandbox launches
- THEN `--net public` SHALL be passed to `msb run`
- AND no inbound port SHALL be published

### Requirement: Per-project package declarations

A non-empty `.tau-packages` file SHALL select image `tau-agent-isolated-<basename>-<package-hash>`, where the hash is derived from the file's raw bytes. The launcher SHALL require interactive approval before building a missing package-specific image.

The file format SHALL:

- contain one Arch Linux package name per line
- strip leading/trailing whitespace and CRLF
- ignore comments and blank lines
- reject shell metacharacters before invoking a build

Comment-only and empty files SHALL use the shared base image.

#### Scenario: Non-interactive rebuild is refused

- GIVEN package changes require a new image
- WHEN stdin is not a terminal
- THEN startup SHALL fail without building

### Requirement: Environment forwarding

Variables named in `TAU_ENV_FILE` (default `~/.env`) SHALL be forwarded as `KEY=value` arguments. Values SHALL NOT be baked into the image or printed by the launcher.

### Requirement: Configurable resources

The sandbox SHALL default to four virtual CPUs, 8 GB memory, and 1024 processes. `TAU_CPUS`, `TAU_MEM`, and `TAU_PIDS` SHALL override these defaults.

### Requirement: Reset

`./run.sh --reset` SHALL remove the project's home, session, and log volumes and exit successfully when any volume is already absent. Host Tau and `.agents` configuration SHALL remain untouched.

### Requirement: Image build and load

When the selected image is absent from the microsandbox cache, the launcher SHALL build it with Podman and load it through `podman save | msb load`. `TAU_IMAGE` SHALL bypass package processing and automatic image management and SHALL be passed to `msb run` unchanged.
