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

The `/workspace` mount SHALL propagate guest-applied permission bits to the host inode (`host-perms=mirror`): a file or directory created or chmod'd inside the sandbox SHALL keep its rwx bits on the host, so scripts created or modified in the sandbox remain executable on the host and git's exec-bit tracking stays consistent. The mirror SHALL cover only ordinary rwx bits; ownership, file type, and setuid/setgid SHALL NOT be propagated, and an owner-access floor SHALL always apply. All other exports SHALL keep microsandbox's default private metadata policy, which materializes guest-created files as owner-only (`600`/`700`) on the host.

#### Scenario: Sandbox-created files keep their modes on the host

- GIVEN the guest creates a regular file and marks a script executable under `/workspace`
- WHEN the host inspects the same files
- THEN the host SHALL observe the same rwx bits the guest set, and a git worktree staged from the guest SHALL stay clean on the host

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
- Host `credentials.json` SHALL NOT be mounted or bootstrapped; the credential file SHALL live in the per-project home volume.
- The host config directory itself SHALL NOT be mounted read-write.

Microsandbox SHALL NOT receive nested mount targets under `/home/tau/.tau`, because its root initialization creates missing parent directories before switching to UID 1000. The entrypoint SHALL create the writable Tau directory first and link the session and log backing paths into it. A root-owned Tau directory left by the earlier nested-mount layout SHALL be moved aside automatically. Non-empty real session or log directories from that layout SHALL be merged into their backing volumes without overwriting existing volume files before links replace them.

On every start, the entrypoint SHALL replace each host-managed `/home/tau/.tau/<name>` with a writable copy of its mounted source. It SHALL track synchronized top-level names and remove a previously synchronized resource when that resource is removed from the host. Project-local entries that were never synchronized from the host SHALL remain persistent. This makes host settings, providers, catalogs, prompts, skills, themes, extensions, and other resources authoritative at startup while preserving host files. It also keeps each atomic config writer's temporary file and destination on the same writable filesystem.

#### Scenario: Fresh Tau home remains writable

- GIVEN microsandbox is starting a new project with empty persistent volumes
- WHEN it prepares session and log mounts before launching UID 1000
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

The launcher SHALL NOT mount host `credentials.json`. A credential update in a guest SHALL leave the project credential file whole: a concurrent reader SHALL observe either the old or the new complete credential, never a partial file.

#### Scenario: Credential update stays whole for readers

- GIVEN a sandbox with a project-local credential file
- WHEN Tau updates a stored credential while another process reads the file
- THEN the reader SHALL observe either the old or the new complete credential
- AND it SHALL NOT observe a partial file

### Requirement: Project-local credential storage

The launcher SHALL NOT mount host `credentials.json` into the guest. Each sandbox SHALL read and write a credential file inside its project persistent home volume. Two sandboxes for different projects SHALL NOT share a credential file. Credential creation and update SHALL occur only in the project volume, never on the host.

#### Scenario: Launch does not mount host credentials

- GIVEN host `credentials.json` exists
- WHEN the launcher starts a sandbox for a project
- THEN the launcher SHALL NOT add a mount for the host credential file
- AND the guest credential path SHALL resolve inside the project home volume

#### Scenario: First-time credential stays in the project volume

- GIVEN a project with no stored credential
- WHEN a sandbox for that project creates a credential
- THEN the credential SHALL be written inside the project home volume
- AND the host config SHALL remain unchanged

#### Scenario: Two projects are isolated

- GIVEN sandboxes for two different projects
- WHEN each sandbox stores a credential
- THEN each credential SHALL persist only in its own project volume

### Requirement: Host login helper produces a readable project credential

A host-side helper SHALL run the OpenAI Codex authorization flow on the host and SHALL produce a credential that a sandbox for the chosen project can load unchanged. The helper SHALL place the credential inside the project home volume. The helper SHALL NOT publish a guest port and SHALL NOT require network access into the guest.

#### Scenario: Browser login completes on the host

- GIVEN a project and a host with a browser
- WHEN the user runs the helper for the project
- THEN the helper SHALL complete the Codex authorization flow against the host callback
- AND the credential SHALL appear inside the project home volume
- AND a sandbox for that project SHALL load the credential unchanged

#### Scenario: Headless host falls back to paste

- GIVEN a host with no browser, or a host whose callback port is occupied
- WHEN the user runs the helper
- THEN the helper SHALL print the authorization URL and accept a pasted redirect URL
- AND a sandbox for that project SHALL load the resulting credential unchanged

### Requirement: Concurrent refresh spends a rotating token once

When two processes share one project credential file, a refresh SHALL spend a rotating refresh token at most once. No process SHALL receive a refresh-token-reused error from concurrent refresh of one shared file.

#### Scenario: Concurrent refresh spends the token once

- GIVEN two processes share one project credential file with an expired token
- WHEN both processes begin a refresh before either refresh completes
- THEN exactly one refresh request SHALL use the stored refresh token
- AND the other process SHALL use the rotated credential written by the winner

### Requirement: Read-only `.agents` resources

When `TAU_AGENTS_DIR` exists, it SHALL be mounted read-only at `/home/tau/.agents`.

#### Scenario: Global skill is usable but immutable

- GIVEN a host `.agents` skill exists
- WHEN the sandbox starts
- THEN Tau SHALL be able to read it
- AND guest writes to the host skill SHALL fail

### Requirement: Reset bypasses secret discovery

A reset invocation SHALL remove the current project's persistent volumes
without locating, opening, sourcing, or validating project-secret
configuration. Secret errors SHALL NOT prevent reset.

#### Scenario: Invalid secrets do not block reset

- GIVEN the exact project secret location contains invalid or unreadable
  entries
- WHEN the user invokes reset
- THEN the launcher SHALL perform its existing volume-removal behavior
- AND it SHALL NOT read or validate either secret source

### Requirement: Exact secret location mapping

The launcher SHALL make the launch directory and projects root absolute,
normalize dot segments lexically, and separately establish their physical
paths by resolving symlinks. Physical location SHALL decide whether a launch
belongs to the projects root: a lexical path outside that physically resolves
inside SHALL be eligible, while a lexical path inside that physically
resolves outside SHALL be ineligible.

`TAU_PROJECTS_DIR` SHALL select the projects root and SHALL default to
`${HOME}/Projects`. An explicitly set empty value SHALL be invalid; a
relative explicit value SHALL resolve from the launch directory; an explicit
value that is not a readable, searchable directory SHALL fail the launch with
a message identifying the setting. When the default root is absent or not a
usable directory, project-secret discovery SHALL be disabled and the launch
SHALL proceed without project secrets.

The launcher SHALL derive project-secret configuration only when the physical
launch directory is a proper descendant of the physical projects root. The
derived directory SHALL mirror the physical relative path under the user's
home, with a dot prefixed to the first relative component. The launcher SHALL
use only the exact derived directory and SHALL NOT inherit from a parent. The
projects root itself and launches outside it SHALL NOT derive project secrets.

#### Scenario: Project maps to hidden home location

- GIVEN the default projects root and a launch from its `megali` child
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali` location

#### Scenario: Nested launch maps exactly

- GIVEN the default projects root and a launch from its `megali/main/api`
  descendant
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali/main/api` location

#### Scenario: Nested launch does not inherit

- GIVEN secret configuration exists for `megali` but not for `megali/main`
- WHEN the launcher starts from exactly `megali/main`
- THEN it SHALL launch without the `megali` secrets

#### Scenario: Explicit root changes mapping

- GIVEN a relative or absolute explicit projects root containing `megali/main`
- WHEN the launcher starts from that descendant
- THEN it SHALL select the user's `.megali/main` location

#### Scenario: Lexically outside launch resolves inside

- GIVEN a lexical launch path outside the lexical projects root physically
  resolves to `megali/main` under the physical projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali/main` location

#### Scenario: Lexically inside launch resolves outside

- GIVEN a lexical launch path inside the lexical projects root physically
  resolves outside the physical projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL launch without automatically selected project secrets

#### Scenario: Root and outside launches have no secrets

- GIVEN the physical launch directory equals or is outside the physical
  projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL launch without automatically selected project secrets

#### Scenario: Unusable default root disables discovery

- GIVEN `TAU_PROJECTS_DIR` is unset and `${HOME}/Projects` is absent,
  dangling, non-directory, unreadable, or unsearchable
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

#### Scenario: Invalid explicit root fails

- GIVEN `TAU_PROJECTS_DIR` is set to an empty value or a path that is not a
  usable directory
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the setting

### Requirement: Paired secret sources

The launcher SHALL treat `secrets.env` (values) and `secrets.yaml` (policy)
in the derived directory as a pair: both SHALL be present readable regular
files, or both SHALL be absent. Exactly one present, or an entry that is a
directory, device, socket, FIFO, unreadable file, or dangling symlink, SHALL
fail the launch with a message identifying the missing or invalid source. A
derived directory with no directory entry SHALL mean no project secrets; a
derived directory entry that is dangling, non-directory, unreadable, or
unsearchable SHALL fail the launch with a message identifying the directory.
Whenever the derived directory entry exists, its physical path SHALL NOT lie
inside the physical projects root (symlink escape); an escaped directory
SHALL fail the launch regardless of whether it contains a pair.

For a present pair, the launcher SHALL source `secrets.env` as trusted shell
with export-all enabled, after `TAU_ENV_FILE` is sourced, so secret values win
over same-named ordinary assignments in the launcher environment that the
runtime inherits. `secrets.yaml` SHALL be passed to the runtime unmodified;
the launcher SHALL NOT parse or validate either file's contents beyond the
reserved-name check below.

A **declared name** is a name assigned by a top-level `NAME=value` or
`export NAME=value` line in `secrets.env`, where `NAME` is a valid shell
identifier. Only declared names participate in the reserved-name check and in
forwarding suppression. A declared name is an **active secret** when the
passed-through policy covers it; the runtime resolves its value from the
inherited launcher environment. A declared name in the reserved set — exactly `HOME`,
`SHELL`, `TERM`, `COLORTERM`, `USER`, `LOGNAME`, `PATH`, `IFS`, `PWD`,
`OLDPWD`, `SHLVL`, `BASH_ENV`, `ENV`, `LD_PRELOAD`, `LD_LIBRARY_PATH`,
`PYTHONHOME`, `PYTHONPATH`, `NODE_OPTIONS`, or any name beginning with `BASH`
or `TAU_` — SHALL fail the launch with a message naming the offending
variable. The image entrypoint SHALL keep all internal shell variables under
the reserved `TAU_ENTRYPOINT_` prefix so reserved user names can never
collide with entrypoint internals.

#### Scenario: Absent directory disables secrets

- GIVEN the exact derived secret directory has no directory entry
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

#### Scenario: Invalid derived directory fails

- GIVEN the derived secret directory entry is a dangling symlink, a regular
  file, or an unreadable or unsearchable directory
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the directory

#### Scenario: Absent pair disables secrets

- GIVEN the derived directory exists and neither exact source entry exists
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

#### Scenario: Incomplete pair fails

- GIVEN exactly one exact source entry exists
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the missing source

#### Scenario: Invalid source type fails

- GIVEN either exact source is a directory, device, socket, FIFO, unreadable
  file, or dangling symlink
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the invalid source

#### Scenario: Secret directory escaping projects root fails

- GIVEN the derived secret directory entry exists and is a symlink resolving
  inside the physical projects root, with or without a pair inside
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the directory

#### Scenario: Secret values win over ordinary assignments

- GIVEN the same name is assigned in `TAU_ENV_FILE` and declared in
  `secrets.env`
- WHEN the launcher sources both files in order
- THEN the runtime process environment SHALL contain the `secrets.env` value
  for that name

#### Scenario: Reserved name fails fast

- GIVEN `secrets.env` declares a reserved name such as `PATH` or `TAU_HOME`
- WHEN the launcher loads the pair
- THEN it SHALL fail with a message naming the offending variable
- AND the sandbox SHALL NOT be created

#### Scenario: Non-assignment lines declare no names

- GIVEN `secrets.env` contains comments, blank lines, or lines without a
  top-level assignment
- WHEN the launcher loads the pair
- THEN those lines SHALL declare no names for the reserved-name check and
  forwarding suppression

#### Scenario: Policy passes through unmodified

- GIVEN a present pair with any `secrets.yaml` content
- WHEN the launcher invokes the runtime
- THEN it SHALL pass that exact file path as `--secret-conf`
- AND it SHALL NOT parse, validate, or regenerate the policy


### Requirement: Protected secret boundary

For each active secret, the runtime SHALL expose a `$MSB_<NAME>` placeholder
to the guest instead of the real value, and SHALL substitute the real value
only as the user's passed-through policy allows. Placeholder generation,
destination matching, DNS observation, TLS identity, HTTP authority, and
violation handling SHALL be governed entirely by the runtime's documented
`--secret-conf` contract; the launcher SHALL NOT encode, default, or rewrite
any policy element. The launcher SHALL NOT place the values of declared
project secrets in guest environment arguments, launcher output, image
inputs, mounts, snapshots, or guest files. Values that reach the launcher
environment through non-declaration shell constructs are outside the
declared-secret contract, as is trusted host configuration that
intentionally reads or prints host secret sources. The destination allowlist
SHALL NOT expand sandbox network policy.

#### Scenario: Guest receives placeholder only

- GIVEN an active secret named `OPENAI_API_KEY`
- WHEN a guest process reads that variable
- THEN it SHALL observe a runtime placeholder
- AND it SHALL NOT observe the real value

#### Scenario: Exactly one secret-conf argument

- GIVEN a present pair
- WHEN the launcher builds the runtime invocation
- THEN exactly one `--secret-conf` argument naming the pair's `secrets.yaml`
  SHALL appear immediately after `run`
- AND every guest argument after the `--` separator SHALL be preserved
  byte-for-byte

#### Scenario: Launch without a pair passes no secret-conf

- GIVEN no present pair
- WHEN the launcher builds the runtime invocation
- THEN it SHALL pass no `--secret-conf` argument

#### Scenario: Secret policy adds no private egress rule

- GIVEN an active policy permits a private destination and no independent
  private-network exception is configured
- WHEN the launcher constructs the runtime invocation
- THEN it SHALL NOT add a network exception for that secret destination

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

Variables named in `TAU_ENV_FILE` (default `${HOME}/.env`) SHALL be forwarded
as guest `KEY=value` arguments except when the name is a declared project
secret; the raw ordinary value for such a name SHALL be omitted regardless of
source order. Ordinary and project values SHALL NOT be baked into the image
or printed by the launcher. Trusted host configuration that intentionally
reads or prints host secret sources is outside the launcher's non-disclosure
guarantee.

#### Scenario: Ordinary variable remains forwarded

- GIVEN an ordinary environment variable is not a declared project secret
- WHEN the sandbox starts
- THEN the guest SHALL receive its real value through ordinary forwarding

#### Scenario: Project secret suppresses raw forwarding

- GIVEN the same name is declared in `TAU_ENV_FILE` and in `secrets.env`,
  and the pair's policy covers that name
- WHEN the launcher constructs guest environment arguments
- THEN it SHALL omit the raw ordinary value for that name
- AND the protected placeholder SHALL be the only guest variable of that name

#### Scenario: Launcher does not print or bake values

- GIVEN ordinary or project values are selected for a launch
- WHEN the launcher builds or starts a sandbox
- THEN it SHALL NOT print those values or include them in image inputs

### Requirement: Configurable resources

The sandbox SHALL default to four virtual CPUs, 8 GB memory, and 1024 processes. `TAU_CPUS`, `TAU_MEM`, and `TAU_PIDS` SHALL override these defaults.

### Requirement: Reset

`./run.sh --reset` SHALL remove the project's home, session, and log volumes and exit successfully when any volume is already absent. Host Tau and `.agents` configuration SHALL remain untouched.

### Requirement: Image build and load

When the selected image is absent from the microsandbox cache, the launcher SHALL build it with Podman and load it through `podman save | msb load`. `TAU_IMAGE` SHALL bypass package processing and automatic image management and SHALL be passed to `msb run` unchanged.

### Requirement: Project secret documentation

User-facing documentation SHALL describe `TAU_PROJECTS_DIR`, the exact host
mapping, no inheritance, the paired-sources contract (sourced `secrets.env`,
runtime-native `secrets.yaml` passed through via `--secret-conf`), the
reserved-name set, ordinary-forwarding suppression, placeholders,
destination-scoped substitution as the runtime's contract, reset behavior,
and network independence. It SHALL stop recommending raw ordinary forwarding
for protected API keys. The sandbox environment reference SHALL distinguish
ordinary forwarded values from protected placeholders and SHALL NOT imply
that `env` reveals protected real values.

#### Scenario: User can configure protected API credentials

- GIVEN a user has a runtime supporting `--secret-conf` and an API credential
  with allowed destinations
- WHEN the user follows the documentation for an exact launch directory
- THEN it SHALL provide enough information to create valid paired sources and
  launch with placeholders

#### Scenario: Guest understands the boundary

- GIVEN a sandbox starts with active project secrets
- WHEN a guest user reads the environment reference
- THEN it SHALL explain placeholders and policy-allowed substitution
- AND it SHALL NOT imply the real values are inspectable in the guest

