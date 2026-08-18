# Tau Sandbox Specification

## Purpose

Defines the behavioral requirements for the Tau coding agent sandbox: a per-project, hardware-isolated microVM built on microsandbox. The sandbox gives Tau a working coding environment while limiting host access to explicit mounts and isolating durable agent state by project.

## Requirements

### Requirement: Per-project persistent state

For a project at resolved path `<path>`, `run.sh` SHALL use the first eight hexadecimal characters of the SHA-256 of `<path>` (including the shell `echo` newline) and the project volume name to derive:

- `tau-persist-<volume-name>-<hash>` mounted at `/home/tau`
- `tau-sessions-<volume-name>-<hash>` mounted at `/var/lib/tau-sandbox/sessions` and linked from `/home/tau/.tau/sessions`
- `tau-logs-<volume-name>-<hash>` mounted at `/var/lib/tau-sandbox/logs` and linked from `/home/tau/.tau/logs`

The project volume name SHALL be the project basename when it is a legal msb volume name (non-empty, only `[A-Za-z0-9._-]`, at most 233 characters so the full volume name fits a 255-byte path component), and otherwise the sanitized basename. The sanitized basename SHALL be the lowercase form in which every run of characters outside `[a-z0-9]` is replaced by a single `_`, with leading and trailing `_` removed and the result truncated to 218 characters; an empty result SHALL become `project`. Uniqueness SHALL be carried by `<hash>`, so sanitization may map distinct basenames to the same name.

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

### Requirement: Project-local config discovery

When `TAU_CONFIG_DIR` is unset, `run.sh` SHALL select as the host Tau config directory the `.tau` entry of the nearest ancestor of the launch directory, where the launch directory itself counts as an ancestor and `.tau` entries are matched as directories with symlinks followed; a dangling `.tau` symlink SHALL NOT match. When no ancestor matches, `${HOME}/.tau` SHALL apply. An explicitly set `TAU_CONFIG_DIR` SHALL take precedence over discovery, and discovery SHALL NOT change the resolved project path used for volume derivation.

#### Scenario: Nearest project root config is discovered

- GIVEN `TAU_CONFIG_DIR` is unset, `<project-root>/.tau` is a directory, and `run.sh` launches from `<project-root>/nested/dir`
- WHEN the launch proceeds
- THEN `<project-root>/.tau` SHALL be the host Tau config directory

#### Scenario: Innermost config wins

- GIVEN `.tau` directories at both `<project-root>` and `<project-root>/nested`, and `run.sh` launches from `<project-root>/nested/dir`
- WHEN the launch proceeds
- THEN `<project-root>/nested/.tau` SHALL be the host Tau config directory

#### Scenario: Discovered config via root symlink

- GIVEN `<project-root>/.tau` is a symlink to directory `<config-world>` and `TAU_CONFIG_DIR` is unset
- WHEN `run.sh` launches from a directory under `<project-root>`
- THEN `<config-world>` SHALL be the host Tau config directory

#### Scenario: No discovery match falls back to the default

- GIVEN no ancestor's `.tau` entry is a directory (absent or a dangling symlink) and `TAU_CONFIG_DIR` is unset
- WHEN `run.sh` launches
- THEN `${HOME}/.tau` SHALL be the host Tau config directory

#### Scenario: Explicit override beats discovery

- GIVEN `<project-root>/.tau` is a directory and `TAU_CONFIG_DIR` is set to `<override-dir>`
- WHEN `run.sh` launches from a directory under `<project-root>`
- THEN `<override-dir>` SHALL be the host Tau config directory

### Requirement: Host Tau config synchronizes into writable project config

When `TAU_CONFIG_DIR` exists, each regular top-level file or directory SHALL be copied to a temporary host-side snapshot and mounted read-only at `/etc/tau-sandbox/bootstrap/tau/<name>`, except credentials, sessions, logs, trust-store files, and synchronization metadata. Symlinks SHALL be recursively dereferenced while creating the snapshot, allowing linked skills, extensions, and other config at any depth to synchronize without exposing broken host-absolute links inside the guest. Dangling symlinks SHALL fail startup, and special files SHALL be ignored:

- Host `sessions` and `logs` SHALL NOT be mounted.
- Host `trust.json`, `trust.json.lock`, and `trust.json.pending` SHALL NOT be mounted; trust state SHALL remain writable in the per-project home because Tau requires a writable lock and atomic replacement.
- The isolated session and log volumes SHALL be mounted under `/var/lib/tau-sandbox/` and linked from their normal Tau paths.
- When host `credentials.json` exists, it SHALL be mounted read-write at `/etc/tau-sandbox/shared/credentials.json` and linked from `/home/tau/.tau/credentials.json`.
- The host config directory itself SHALL NOT be mounted read-write.

Microsandbox SHALL NOT receive nested mount targets under `/home/tau/.tau`, because its root initialization creates missing parent directories before switching to UID 1000. The entrypoint SHALL create the writable Tau directory first and link the external credential, session, and log backing paths into it. A root-owned Tau directory left by the earlier nested-mount layout SHALL be moved aside automatically. Non-empty real session or log directories from that layout SHALL be merged into their backing volumes without overwriting existing volume files before links replace them.

On every start, the entrypoint SHALL replace each host-managed `/home/tau/.tau/<name>` with a writable copy of its mounted source. It SHALL track synchronized top-level names and remove a previously synchronized resource when that resource is removed from the host. Project-local entries that were never synchronized from the host SHALL remain persistent. This makes host settings, providers, catalogs, prompts, skills, themes, extensions, and other resources authoritative at startup while preserving host files. It also keeps each atomic config writer's temporary file and destination on the same writable filesystem.

#### Scenario: Fresh Tau home remains writable

- GIVEN microsandbox is starting a new project with empty persistent volumes
- WHEN it prepares credential, session, and log mounts before launching UID 1000
- THEN none of those mount targets SHALL create `/home/tau/.tau`
- AND the entrypoint SHALL create a writable Tau directory and normal-path links

#### Scenario: Linked host resource seeds writable project state

- GIVEN a symlink within host `~/.tau/skills` targets a directory outside `~/.tau`
- WHEN the project sandbox starts
- THEN the target SHALL be dereferenced into the temporary host-side snapshot
- AND the guest bootstrap and writable config paths SHALL contain an ordinary directory rather than the host symlink

#### Scenario: Host resource seeds writable project state

- GIVEN host `~/.tau/settings.json` exists
- WHEN the project sandbox starts for the first time
- THEN Tau SHALL read a copied value from `/home/tau/.tau/settings.json`
- AND a guest write to that project-local path SHALL succeed
- AND the host file SHALL remain unchanged

#### Scenario: Host changes synchronize on restart

- GIVEN a sandbox has synchronized and modified its writable project-local copy
- WHEN the host config changes and the same project starts another sandbox
- THEN the project-local copy SHALL contain the current host value
- AND newly added host skills and extensions SHALL be available
- AND a previously synchronized resource removed from the host SHALL be removed locally
- AND an entry created only inside the sandbox SHALL remain persistent

#### Scenario: Atomic provider replacement succeeds

- GIVEN host `providers.json` seeded a project-local copy
- WHEN Tau writes a sibling temporary file and renames it over `/home/tau/.tau/providers.json`
- THEN the replacement SHALL succeed without `EBUSY`
- AND host `providers.json` SHALL remain unchanged

#### Scenario: Host history and trust are not exposed

- GIVEN host sessions, logs, and trust-store files contain data
- WHEN the sandbox starts
- THEN host sessions, logs, and trust-store files SHALL NOT be mounted as bootstrap sources
- AND sessions, logs, and trust SHALL use writable per-project state

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
- allow egress to exactly the hosts listed in `TAU_LAN_HOSTS` (comma-separated, empty by default) without allowing the rest of the private network

#### Scenario: Guest identity is unprivileged

- WHEN `id -u` runs in the guest
- THEN it SHALL print `1000`

#### Scenario: External DNS and configured LAN hosts are reachable while inbound remains closed

- WHEN the sandbox launches
- THEN `--net public` SHALL be passed to `msb run`
- AND one `--net-rule allow@<host>` SHALL be passed per non-empty `TAU_LAN_HOSTS` entry
- AND an unset or empty `TAU_LAN_HOSTS` SHALL pass no `--net-rule`
- AND a `TAU_LAN_HOSTS` value containing characters outside `[0-9A-Za-z.:-]` SHALL abort the launch with an error
- AND the broad `private` network profile SHALL NOT be enabled
- AND no inbound port SHALL be published

### Requirement: Per-project package declarations

For this requirement, a `.tau-packages` file is non-empty when it declares at least one package name after stripping comments, blank lines, and surrounding whitespace. A non-empty `.tau-packages` file SHALL select image `tau-agent-isolated-<image-name>-<base-hash>-<package-hash>`, where:

- `<image-name>` is the project basename when `tau-agent-isolated-<basename>-<8 hex>-<8 hex>` is a legal OCI reference path component (lowercase `[a-z0-9._-]` with no adjacent separators, at most 255 characters), and otherwise the sanitized basename;
- `<package-hash>` is derived from the raw bytes of `.tau-packages`;
- `<base-hash>` is the first eight hexadecimal characters of the SHA-256 of the text formed by concatenating, in lexicographic path order, the hex-encoded SHA-256 digests (64 lowercase hex characters, no separators) of the raw bytes of each regular file directly under `config/` (including dotfiles), preceded by the digest of the repository `Containerfile`. It SHALL change when the content, or set, of those files changes and SHALL be stable when none does. Non-regular entries under `config/` SHALL be ignored.

The launcher SHALL require interactive approval before building a missing package-specific image. When the launcher would otherwise derive a package image tag (a non-empty `.tau-packages` file is present and no `TAU_IMAGE` override is set), a missing repository `Containerfile` or `config/` directory SHALL abort the launch with an error rather than derive a tag whose freshness cannot be verified against the current inputs.

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

#### Scenario: Base input change invalidates the package image

- GIVEN a project whose package image was built from an earlier build context
- WHEN the content of `Containerfile` or a `config/` file changes and the `.tau-packages` content does not
- THEN the launcher SHALL select a different image tag than the previously built one
- AND building it SHALL require the same interactive approval as any missing package image

#### Scenario: Added base input changes the tag

- GIVEN a package image was tagged from a base-input set without a particular regular file under `config/`
- WHEN that file is added to `config/` without changing any other input
- THEN the launcher SHALL select a different image tag than the previously built one

#### Scenario: Removed base input changes the tag

- GIVEN a package image was tagged from a base-input set that included a particular regular file under `config/`
- WHEN that file is removed without changing any other input
- THEN the launcher SHALL select a different image tag than the previously built one

#### Scenario: Non-file config entries do not affect the hash

- GIVEN `config/` contains a directory alongside its regular files and the package image tag exists in the cache
- WHEN the project launches
- THEN the launcher SHALL select the same tag as it would without the directory
- AND it SHALL boot the cached image without building

#### Scenario: Unchanged inputs reuse the cached image

- GIVEN the package image tag derived from the current build context exists in the microsandbox cache
- WHEN the project launches and stdin is not a terminal
- THEN the launcher SHALL NOT build anything
- AND the launcher SHALL NOT remove any image from the cache
- AND the launcher SHALL boot the cached image

#### Scenario: Non-interactive base-triggered rebuild is refused

- GIVEN the package image tag is missing because the build context changed and stdin is not a terminal
- WHEN the project launches
- THEN startup SHALL fail without building

#### Scenario: Missing base inputs abort a package-tag launch

- GIVEN the project has a non-empty `.tau-packages` file and no `TAU_IMAGE` override, and the repository `Containerfile` or the `config/` directory is missing
- WHEN the project launches
- THEN the launcher SHALL abort with an error and SHALL NOT build or boot an image

#### Scenario: Missing base inputs do not affect other launches

- GIVEN the repository `Containerfile` or the `config/` directory is missing
- WHEN a project without a non-empty `.tau-packages` file, or with a `TAU_IMAGE` override, launches
- THEN the launcher SHALL proceed with the shared base image or the override and SHALL NOT abort

### Requirement: Superseded package images are pruned

Package image tags are keyed by image name, base hash, and package hash, so same-image-name projects share one tag namespace: projects with identical base inputs and identical `.tau-packages` content use the same tag, while different package contents produce different tags under the same image-name prefix.

When the launcher builds a package-specific image, it SHALL remove from the microsandbox cache every other image whose reference is `localhost/tau-agent-isolated-<image-name>-<package-hash>:latest` (legacy single-hash form of the current package content) or `localhost/tau-agent-isolated-<image-name>-<8 hex>-<package-hash>:latest` (any base version of the current package content). It SHALL NOT remove the image it just loaded. Inherent to the shared tag namespace, an image carrying the current package hash at another base hash is removed whether this project or a same-image-name project with identical `.tau-packages` content produced it. Images tagged with any other package hash — including those of same-image-name projects with different `.tau-packages` content and those of earlier package contents of this project — SHALL NOT be removed; in particular, a legacy single-hash tag whose hash differs from the current package hash SHALL NOT be removed. A failed removal SHALL NOT fail the build, the load, or the launch, and SHALL NOT be reported as an error.

#### Scenario: Base-triggered rebuild removes the superseded image

- GIVEN the cache contains a package image for the current package content whose tag derives from an older base hash (two-hash form)
- WHEN the launcher rebuilds the package image for the changed base
- THEN the old image SHALL be removed from the cache
- AND the newly built image SHALL remain

#### Scenario: Legacy single-hash image is pruned

- GIVEN the cache contains a package image tagged in the legacy single-hash form for the same project and the same package content
- WHEN the launcher rebuilds the package image
- THEN the legacy image SHALL be removed from the cache
- AND the newly built image SHALL remain

#### Scenario: Legacy tag of another content is kept

- GIVEN the cache contains a package image tagged in the legacy single-hash form whose hash differs from the current package hash
- WHEN the launcher rebuilds the package image
- THEN that image SHALL remain in the cache

#### Scenario: Same-image-name projects keep their package images

- GIVEN two projects with the same image name and different `.tau-packages` contents both have cached package images
- WHEN the launcher rebuilds the package image for one of them
- THEN the other project's image SHALL remain in the cache

#### Scenario: Earlier package content image survives a rebuild

- GIVEN the cache contains a package image tagged from earlier `.tau-packages` content of the same project
- WHEN the launcher rebuilds the package image for the current content
- THEN the earlier-content image SHALL remain in the cache

#### Scenario: Fresh build with an empty cache succeeds

- GIVEN the cache contains no package images for the project
- WHEN the launcher builds the package image for the first time
- THEN the build and load SHALL succeed and no removal error SHALL be reported

#### Scenario: Failed removal does not fail the launch

- GIVEN the cache contains a superseded package image whose removal from the cache fails
- WHEN the launcher rebuilds the package image for the changed base
- THEN the build, the load, and the launch SHALL succeed
- AND no removal error SHALL be reported

### Requirement: Environment forwarding

Variables named in `TAU_ENV_FILE` (default `~/.env`) SHALL be forwarded as `KEY=value` arguments. Values SHALL NOT be baked into the image or printed by the launcher.

### Requirement: Configurable resources

The sandbox SHALL default to four virtual CPUs, 8 GB memory, and 1024 processes. `TAU_CPUS`, `TAU_MEM`, and `TAU_PIDS` SHALL override these defaults.

### Requirement: Reset

`./run.sh --reset` SHALL remove the project's home, session, and log volumes and exit successfully when any volume is already absent. Host Tau and `.agents` configuration SHALL remain untouched.

### Requirement: Image build and load

When the selected image is absent from the microsandbox cache, the launcher SHALL build it with Podman and load it through `podman save | msb load`. `TAU_IMAGE` SHALL bypass package processing and automatic image management and SHALL be passed to `msb run` unchanged.
