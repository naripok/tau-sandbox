# Tau Sandbox Specification

## Purpose

Defines the behavioral requirements for the Tau coding agent sandbox: a per-project, hardware-isolated microVM environment built on microsandbox. The sandbox must give the agent a working coding environment while keeping the host, other projects, and agent AI access strictly separated.

## Requirements

### Requirement: Per-project persistent volume naming

When `run.sh` is invoked from a project directory, it SHALL derive a persistent volume name from the project path: `tau-persist-<basename>-<hash>` where `<hash>` is the first 8 hex characters of the SHA-256 of the resolved project path, and `<basename>` is the directory basename.

#### Scenario: Volume name is deterministic per project

- GIVEN a project at `/home/me/Projects/alpha`
- WHEN the sandbox is launched
- THEN the volume name SHALL start with `tau-persist-alpha-` and match the hash of the resolved path

#### Scenario: Two projects get distinct volumes

- GIVEN projects `alpha` and `beta` at different paths
- WHEN both sandboxes are launched
- THEN their volume names SHALL differ (different basename and/or different hash)

### Requirement: Ephemeral microVM with persistent home

Each `tau-sandbox` invocation SHALL boot a fresh microVM from the configured image and discard it when the command finishes. Durable state SHALL live in the persistent named volume mounted at `/home/tau` and in the project bind mount at `/workspace`.

#### Scenario: Files written to /home/tau survive across runs

- GIVEN a sandbox run that writes `hello.txt` to `/home/tau`
- WHEN a second run of the same project executes
- THEN `/home/tau/hello.txt` SHALL exist with the same content

#### Scenario: Rootfs writes do not survive

- GIVEN a run that writes a file to a rootfs path such as `/etc/sandbox-marker.txt`
- WHEN a second run of the same project executes
- THEN `/etc/sandbox-marker.txt` SHALL NOT exist

### Requirement: Workspace bind mount

The current working directory SHALL be mounted read-write at `/workspace` and set as the working directory.

#### Scenario: Host files are visible in the sandbox

- GIVEN a host directory containing `agent.txt`
- WHEN the sandbox is launched from that directory
- THEN `cat /workspace/agent.txt` in the sandbox SHALL succeed

#### Scenario: Gust writes land on the host

- GIVEN a sandbox run that writes `generated.txt` to `/workspace`
- WHEN the run exits
- THEN `generated.txt` SHALL exist in the host directory

### Requirement: Read-only host config mounts

When the host `~/.tau` directory exists, it SHALL be mounted read-only at `/tau-source`. When the host `~/.agents` directory exists, it SHALL be mounted read-only at `/agents-source`. Writes to either mount SHALL fail even from a root process inside the guest (enforced host-side).

#### Scenario: Config mount is read-only

- GIVEN a host `~/.tau` containing a skills directory
- WHEN a sandbox run attempts `echo x > /tau-source/skills/evil.md`
- THEN the write SHALL fail

### Requirement: Config sync into the volume

On every start, the entrypoint SHALL rsync host config from `/tau-source` into the volume's `~/.tau/` and from `/agents-source` into the volume's `~/.agents/`, excluding sessions, logs, credentials, trust data, and lock files.

#### Scenario: New host skill appears in the volume

- GIVEN a host `~/.tau/skills/new-skill.md` not present in the volume
- WHEN the sandbox starts
- THEN `~/.tau/skills/new-skill.md` SHALL exist in the volume

#### Scenario: Host sessions are never copied

- GIVEN a host `~/.tau/sessions/host-only.jsonl`
- WHEN the sandbox starts
- THEN the file SHALL NOT exist in the volume

### Requirement: Environment reference injection

The entrypoint SHALL place an `APPEND_SYSTEM.md` in the volume's `~/.tau/` on every start, preferring `/workspace/config/APPEND_SYSTEM.md` (repo checkout) and falling back to the image copy at `/etc/tau-sandbox/APPEND_SYSTEM.md`. Tau auto-injects this file into the system prompt.

#### Scenario: Workspace copy wins

- GIVEN `/workspace/config/APPEND_SYSTEM.md` exists
- AND `/etc/tau-sandbox/APPEND_SYSTEM.md` exists and differs
- WHEN the sandbox starts
- THEN `~/.tau/APPEND_SYSTEM.md` SHALL match the workspace copy

### Requirement: Per-project package declarations (`.tau-packages`)

When a `.tau-packages` file exists in the current working directory and lists packages, the system SHALL derive a per-project image name `tau-agent-isolated-<basename>-<hash>` where `<hash>` is a hash of the `.tau-packages` raw bytes, build that image with the packages installed, and require explicit user approval before building when stdin is a terminal.

#### Scenario: Packages hash into the image name

- GIVEN a project named `myproject` with `.tau-packages` containing `cmake`
- WHEN the sandbox is launched (approved)
- THEN the image SHALL be `localhost/tau-agent-isolated-myproject-<hash>:latest`

#### Scenario: Comment-only file uses the shared base image

- GIVEN `.tau-packages` containing only comments and blank lines
- WHEN the sandbox is launched
- THEN the shared base image `localhost/tau-agent-isolated:latest` SHALL be used

#### Scenario: Dangerous characters are rejected

- GIVEN `.tau-packages` containing `cmake; rm -rf /`
- WHEN the sandbox is launched
- THEN the launch SHALL fail with an error and SHALL NOT build an image

### Requirement: Package declaration file format

The system SHALL accept a `.tau-packages` file containing one Arch package name per line. Leading/trailing whitespace SHALL be stripped. Lines beginning with `#` (after stripping) are comments. Blank lines SHALL be ignored. Trailing `\r` SHALL be stripped.

### Requirement: Environment variable forwarding

Variables defined in `~/.env` (or the file at `TAU_ENV_FILE`) SHALL be forwarded into the sandbox as environment variables. The values SHALL NOT be written into the image or echoed by `run.sh`.

#### Scenario: API keys arrive in the sandbox

- GIVEN `~/.env` containing `VLLM_API_KEY=sk-test`
- WHEN the sandbox runs `env`
- THEN `VLLM_API_KEY=sk-test` SHALL be present

### Requirement: Security posture

The sandbox SHALL run as the unprivileged user `tau` (uid 1000) with the `restricted` microsandbox security profile, inbound network SHALL be denied, outbound internet SHALL be allowed, and the process limit SHALL be capped.

#### Scenario: Runs as unprivileged user

- GIVEN a running sandbox
- WHEN `id -u` is executed inside
- THEN it SHALL print `1000`

#### Scenario: Inbound denied

- GIVEN a running sandbox launch that includes `--net-default-ingress deny`
- THEN the flag SHALL be present in the `msb run` invocation

### Requirement: Reset

`./run.sh --reset` SHALL remove the project's persistent volume.

#### Scenario: Reset removes volume

- GIVEN a project with an existing persistent volume
- WHEN `./run.sh --reset` is executed
- THEN the volume SHALL be removed and the command SHALL exit 0

### Requirement: Image build and load

When the configured image reference is not present in the microsandbox cache, `run.sh` and `install.sh` SHALL build it with podman from the repository `Containerfile` and load it with `podman save | msb load`. When `TAU_IMAGE` is set, no build SHALL be attempted and the reference is passed through to `msb run` unchanged.

#### Scenario: Missing image triggers build and load

- GIVEN an empty microsandbox image cache
- WHEN `run.sh` is invoked from a project
- THEN `podman build` SHALL run and `msb load` SHALL be invoked via the save pipe

#### Scenario: TAU_IMAGE bypasses builds

- GIVEN `TAU_IMAGE=custom:tag`
- WHEN `run.sh` is invoked
- THEN no `podman build` SHALL run and `msb run ... custom:tag` SHALL be invoked
