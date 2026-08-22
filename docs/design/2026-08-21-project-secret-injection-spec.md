# Spec: Project Secret Injection

## Domain: Sandbox Launch

### ADDED Requirements

#### Requirement: Reset bypasses secret discovery

A reset invocation SHALL remove the current project's persistent volumes without locating, opening, parsing, validating, or exporting project-secret configuration. Secret errors and runtime compatibility SHALL NOT prevent reset.

##### Scenario: Invalid secrets do not block reset

- GIVEN the exact project secret location contains invalid or unreadable entries
- WHEN the user invokes reset
- THEN the launcher SHALL perform its existing volume-removal behavior
- AND it SHALL NOT read or validate either secret source

##### Scenario: Unsupported runtime does not block reset

- GIVEN the installed runtime is outside the project-secret compatibility range
- WHEN the user invokes reset
- THEN the launcher SHALL perform its existing volume-removal behavior without a secret compatibility error

#### Requirement: Exact secret location mapping

The launcher SHALL make the launch directory and projects root absolute and lexically normalize dot segments without following symlinks. It SHALL separately establish their physical paths by resolving symlinks. Containment SHALL compare complete path components in both lexical and physical forms and SHALL fail closed when identity or containment cannot be established. Physical location SHALL decide whether a launch belongs to the projects root: a lexical path outside that physically resolves inside SHALL be eligible, while a lexical path inside that physically resolves outside SHALL be ineligible. Independent lexical and physical checks SHALL both apply when validating secret source placement.

`TAU_PROJECTS_DIR` SHALL select the projects root and SHALL default to `${HOME}/Projects`. An explicitly set empty value SHALL be invalid. A relative explicit value SHALL resolve from the launch directory. An absent default root SHALL disable project-secret discovery. A default or explicit root with a directory entry SHALL resolve to a readable and searchable directory; otherwise launch SHALL fail before sandbox creation.

The launcher SHALL derive project-secret configuration only when the physical launch directory is a proper descendant of the physical projects root. The host location SHALL mirror the physical relative path under the user's home, with a dot prefixed to the first relative component. The launcher SHALL use only the exact location and SHALL NOT inherit from a parent. The projects root itself and launches outside it SHALL NOT derive project secrets.

##### Scenario: Project maps to hidden home location

- GIVEN the default projects root and a launch from its `megali` child
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali` location

##### Scenario: Nested launch maps exactly

- GIVEN the default projects root and a launch from its `megali/main/api` descendant
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali/main/api` location

##### Scenario: Nested launch does not inherit

- GIVEN secret configuration exists for `megali` but not for `megali/main`
- WHEN the launcher starts from exactly `megali/main`
- THEN it SHALL launch without the `megali` secrets

##### Scenario: Explicit root changes mapping

- GIVEN a relative or absolute explicit projects root containing `megali/main`
- WHEN the launcher starts from that descendant
- THEN it SHALL select the user's `.megali/main` location

##### Scenario: Lexically outside launch resolves inside

- GIVEN a lexical launch path outside the lexical projects root physically resolves to `megali/main` under the physical projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali/main` location

##### Scenario: Lexically inside launch resolves outside

- GIVEN a lexical launch path inside the lexical projects root physically resolves outside the physical projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL launch without automatically selected project secrets

##### Scenario: Missing default disables discovery

- GIVEN `TAU_PROJECTS_DIR` is unset and `${HOME}/Projects` has no directory entry
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

##### Scenario: Invalid default fails

- GIVEN `TAU_PROJECTS_DIR` is unset and `${HOME}/Projects` is dangling, non-directory, unreadable, or unsearchable
- WHEN the launcher starts
- THEN it SHALL fail before sandbox creation

##### Scenario: Invalid explicit root fails

- GIVEN `TAU_PROJECTS_DIR` is empty, missing, dangling, non-directory, unreadable, or unsearchable
- WHEN the launcher starts
- THEN it SHALL fail before sandbox creation with an error that identifies the setting

##### Scenario: Root and outside launches have no secrets

- GIVEN the physical launch directory equals or is outside the physical projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL launch without automatically selected project secrets

#### Requirement: Early source preflight

For normal launches, project-secret discovery and exposure preflight SHALL finish before image lookup, image build, ordinary environment-file evaluation, host configuration snapshotting, or mount construction. A derived secret location with no directory entry SHALL mean no project secrets. An existing entry SHALL resolve to a readable and searchable directory or launch SHALL fail.

Within an existing directory, the exact source entries SHALL be `${mapped-directory}/secrets.env` for values and `${mapped-directory}/secrets.yaml` for policy. Both entries SHALL be readable regular files or both SHALL be absent. Dangling symlinks and non-regular entries SHALL count as present and invalid. Two present regular sources SHALL form a present pair and trigger runtime compatibility checking before content parsing. A present pair whose grammars and exact name sets pass SHALL form a validated configuration, which SHALL contain at least one matched secret. Validated entries supplied to the runtime SHALL be active secrets. Empty, comment-only, or malformed present pairs SHALL fail after the compatibility gate.

##### Scenario: Preflight precedes image build

- GIVEN sources belonging to a present pair would be exposed by the image build context
- WHEN the selected image is absent
- THEN the launcher SHALL reject the sources before invoking the image builder

##### Scenario: Absent directory disables secrets

- GIVEN the exact derived secret directory has no directory entry
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

##### Scenario: Invalid directory fails closed

- GIVEN the derived secret directory is dangling, non-directory, unreadable, or unsearchable
- WHEN the launcher starts
- THEN it SHALL fail before image or sandbox creation

##### Scenario: Exact basenames form the pair

- GIVEN a derived directory contains readable regular `secrets.env` and `secrets.yaml` entries
- WHEN the launcher performs source preflight
- THEN it SHALL classify those exact entries as the present value and policy pair
- AND differently named files SHALL NOT substitute for either source

##### Scenario: Absent pair disables secrets

- GIVEN the derived directory is valid and neither exact source entry exists
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

##### Scenario: Incomplete pair fails closed

- GIVEN exactly one exact source entry exists
- WHEN the launcher starts
- THEN it SHALL fail before image or sandbox creation and identify the missing source

##### Scenario: Invalid source type fails closed

- GIVEN either exact source is a directory, device, socket, FIFO, unreadable file, or dangling symlink
- WHEN the launcher starts
- THEN it SHALL fail before opening any blocking special file

##### Scenario: Empty pair fails closed

- GIVEN both exact sources contain no declared secret after blank and full-comment lines are ignored
- WHEN the launcher starts
- THEN it SHALL fail before image or sandbox creation

#### Requirement: Host-only source isolation

The source directory and files belonging to a present pair SHALL be lexically and physically outside the projects root. Lexical containment SHALL use normalized absolute path components without symlink resolution. Physical containment and file aliases SHALL use filesystem identity, including hard links and case-insensitive aliases. An inability to inspect identity SHALL fail closed.

The launcher SHALL reject present-pair sources when the directory or either file overlaps in either direction with the workspace, image build context, a direct guest mount source, a host configuration source, shared credentials, the immutable environment reference, or a target reachable from a recursively dereferenced host-configuration link. It SHALL detect hard-link aliases of either source throughout recursively exposed or built source trees. Neither source nor real value SHALL be mounted, copied, snapshotted, built into an image, or otherwise made readable inside the sandbox by the launcher.

##### Scenario: Lexical project source is rejected

- GIVEN an exact source is lexically inside the projects root but resolves outside it
- WHEN the launcher starts
- THEN it SHALL fail before image or sandbox creation

##### Scenario: Physical project source is rejected

- GIVEN an exact source is lexically outside the projects root but resolves inside it
- WHEN the launcher starts
- THEN it SHALL fail before image or sandbox creation

##### Scenario: Direct source overlap is rejected

- GIVEN a present-pair directory overlaps the workspace or another direct host source in either direction
- WHEN the launcher starts
- THEN it SHALL fail without mounting or building the source

##### Scenario: Nested snapshot link is rejected

- GIVEN a snapshotted host source contains a nested symlink whose resolved target overlaps a present-pair directory or file
- WHEN the launcher starts
- THEN it SHALL fail without copying the target

##### Scenario: Hard-link exposure is rejected

- GIVEN a hard link to either present-pair source exists anywhere under a recursively mounted, snapshotted, or built host source
- WHEN the launcher starts
- THEN it SHALL fail before image or sandbox creation

##### Scenario: Exposure error has first precedence

- GIVEN present-pair sources have an exposure collision, malformed content, and an incompatible runtime
- WHEN the launcher starts
- THEN it SHALL report the exposure failure without checking compatibility or parsing content

##### Scenario: Secret sources remain absent from guest data

- GIVEN a validated configuration with no exposure collision
- WHEN the sandbox starts
- THEN the launcher SHALL NOT place either source or real value in the sandbox filesystem, mounts, snapshots, or image inputs

#### Requirement: Literal secret value grammar

The value source SHALL contain only printable ASCII bytes plus CR and LF line endings. It SHALL reject NUL, other control bytes, and non-ASCII bytes before storing or exporting values. It SHALL accept LF or CRLF, remove a CR only as part of CRLF, accept a final line without a terminator, ignore empty and space-only lines, and ignore full-line comments whose first non-space character is `#`. Tabs SHALL be rejected.

Every other line SHALL be `NAME=VALUE`, with a name beginning in column one and matching `[A-Za-z_][A-Za-z0-9_]*`. All characters after the first equals sign SHALL be literal value data. Zero-length values and duplicate names SHALL be rejected; space-only values SHALL be accepted. Shell syntax, interpolation, substitutions, and quote removal SHALL NOT be evaluated. Errors SHALL report the source, line number when available, and error class without printing the line or value.

The image entrypoint SHALL keep all internal shell variables under the reserved `TAU_ENTRYPOINT_` prefix. The launcher SHALL reject the following exact guest names: `HOME`, `SHELL`, `TERM`, `COLORTERM`, `USER`, `LOGNAME`, `PATH`, `PYTHONUSERBASE`, `NPM_CONFIG_PREFIX`, `PIP_USER`, `TAU_NO_UPDATE_CHECK`, `TAU_SANDBOX_SHARED_CREDENTIALS`, `BASH_ENV`, `ENV`, `IFS`, `CDPATH`, `GLOBIGNORE`, `SHELLOPTS`, `BASHOPTS`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONHOME`, `PYTHONPATH`, `NODE_OPTIONS`, `PWD`, `OLDPWD`, `SHLVL`, `_`, `EUID`, `UID`, `PPID`, `BASHPID`, `LINENO`, `FUNCNAME`, `GROUPS`, `DIRSTACK`, `PIPESTATUS`, `RANDOM`, `SECONDS`, `HOSTNAME`, `HOSTTYPE`, `MACHTYPE`, `OSTYPE`, `OPTERR`, `OPTIND`, `OPTARG`, `PS1`, `PS2`, `PS4`, `EPOCHSECONDS`, `EPOCHREALTIME`, `SRANDOM`, `REPLY`, `MAPFILE`, `COPROC`, `HISTCMD`, `COLUMNS`, and `LINES`. It SHALL also reject names beginning `BASH`, `TAU_ENTRYPOINT_`, or `TAU_SANDBOX_SECRET_SOURCE_`.

##### Scenario: Literal value is preserved

- GIVEN a value contains spaces, dollar signs, quotes, `#`, and additional equals signs
- WHEN the launcher reads the source
- THEN it SHALL retain those characters as literal data

##### Scenario: NUL and unsupported bytes are rejected safely

- GIVEN the source contains NUL, another unsupported control byte, or a non-ASCII byte
- WHEN the launcher starts
- THEN it SHALL fail before storing or exporting project values
- AND it SHALL NOT print the offending data

##### Scenario: Shell syntax is not executed

- GIVEN a value contains command-substitution syntax
- WHEN the launcher reads the source
- THEN it SHALL retain that syntax as data without executing it

##### Scenario: Line forms are handled exactly

- GIVEN the source contains blank lines, space-only lines, full-line comments, CRLF assignments, and a final unterminated assignment
- WHEN the launcher reads the source
- THEN it SHALL ignore non-entries, remove only CRLF carriage returns, and retain the final assignment

##### Scenario: Malformed entry is rejected safely

- GIVEN an entry has a tab, leading name whitespace, invalid name, or no equals sign
- WHEN the launcher starts
- THEN it SHALL fail with the line number without printing the entry

##### Scenario: Duplicate and empty values are rejected

- GIVEN an entry duplicates a name or has a zero-length value
- WHEN the launcher starts
- THEN it SHALL fail with the line number without printing values

##### Scenario: Space-only value is accepted

- GIVEN an entry has a value containing only spaces
- WHEN the launcher reads the source
- THEN it SHALL retain those spaces as non-empty literal data

##### Scenario: Entrypoint namespace is reserved

- GIVEN an entry uses the `TAU_ENTRYPOINT_` prefix
- WHEN the launcher starts
- THEN it SHALL fail before sandbox creation with the line number and reserved-name error class
- AND the entrypoint SHALL keep its internal variables under that prefix

##### Scenario: Bash dynamic name is reserved

- GIVEN an entry uses an exact Bash-managed name or the `BASH` prefix
- WHEN the launcher starts
- THEN it SHALL fail before sandbox creation with the line number and reserved-name error class
- AND it SHALL NOT print the name, line, or value

##### Scenario: Runner name is reserved

- GIVEN an entry uses an exact runner-owned name or the host-source prefix
- WHEN the launcher starts
- THEN it SHALL fail before sandbox creation with the line number and reserved-name error class
- AND it SHALL NOT print the name, line, or value

#### Requirement: Restricted secret policy grammar

The policy source SHALL be ASCII and SHALL reject NUL, BOM, non-ASCII bytes, and tabs. It SHALL accept LF or CRLF, remove a CR only as part of CRLF, and accept a final grammar line without a terminator. Empty lines, space-only lines, and full-line comments beginning after zero or more spaces MAY occur between grammar elements and SHALL NOT change parser state. Inline comments SHALL be rejected.

A secret header SHALL be exactly `NAME:` in column one with no trailing spaces, using the value-name and reserved-name rules. Its first and only required field SHALL be exactly two spaces followed by `allow:`. One or more allow items SHALL immediately follow after ignored lines, each exactly four spaces followed by `- ` and one scalar. An optional `inject:` field SHALL occur at most once after the allow list with the same two-space field indentation and one or more four-space list items. Injection values SHALL be exactly `headers`, `basic_auth`, or `query_params`; an omitted field SHALL mean `headers`. No other indentation, trailing whitespace, field order, field, or syntax SHALL be accepted.

An allow scalar MAY be unquoted or surrounded by one matching pair of single or double quotes with no escapes. The quotes SHALL contain the complete scalar. After unquoting, an exact destination SHALL contain two or more ASCII DNS labels; a wildcard SHALL be `*.` followed by two or more labels. Each label SHALL be 1–63 characters, begin and end with an ASCII letter or digit, and contain only ASCII letters, digits, and internal hyphens. An exact hostname SHALL be at most 253 characters. A wildcard suffix SHALL be at most 253 characters, making the complete `*.` scalar at most 255 characters. Destinations SHALL be canonicalized to lowercase. Empty values, `*`, IP literals, ports, interpolation markers, and every other character SHALL be rejected.

Names, fields, and injection values SHALL compare case-sensitively. Canonical destinations SHALL compare case-insensitively through their lowercase form. Duplicate names, fields, destinations, or injection values SHALL be rejected. The value and policy name sets SHALL match exactly and contain at least one name. Errors SHALL report the source, line number, and error class without printing secret values.

##### Scenario: Minimal policy is accepted

- GIVEN a policy has one name, one allow field with one valid destination, and no inject field
- WHEN the launcher reads it
- THEN it SHALL enable header substitution for that name

##### Scenario: Full supported policy is accepted

- GIVEN a policy uses comments and blank lines between valid elements, quoted and unquoted destinations, a wildcard, and all three supported injection locations
- WHEN the launcher reads it
- THEN it SHALL canonicalize destinations and retain the enabled locations

##### Scenario: Final unterminated policy line is accepted

- GIVEN the final valid policy item has no line terminator
- WHEN the launcher reads the policy
- THEN it SHALL retain that complete item

##### Scenario: Grammar boundaries are rejected

- GIVEN a policy contains a BOM, tab, non-ASCII byte, trailing whitespace, bad indentation, wrong field order, inline comment, inline collection, anchor, alias, tag, unknown field, escape, unmatched quote, empty scalar, or interpolation marker
- WHEN the launcher starts
- THEN it SHALL fail with the line number without generating runtime configuration

##### Scenario: Destination length boundaries are accepted

- GIVEN an exact destination is exactly 253 characters or a wildcard suffix is exactly 253 characters with a 255-character complete scalar, and every label is valid
- WHEN the launcher reads the policy
- THEN it SHALL accept the destination

##### Scenario: Invalid destination is rejected

- GIVEN an allow item is `*`, an IP literal, port-qualified, single-label, has an invalid DNS label, has an exact name over 253 characters, or has a wildcard suffix over 253 characters
- WHEN the launcher starts
- THEN it SHALL fail with the line number

##### Scenario: Missing list data is rejected

- GIVEN a policy name has no allow item or has an inject field with no item
- WHEN the launcher starts
- THEN it SHALL fail before sandbox creation

##### Scenario: Duplicate policy data is rejected

- GIVEN a policy repeats a name, field, canonical destination, or injection location
- WHEN the launcher starts
- THEN it SHALL fail with the duplicate line number

##### Scenario: Unsupported injection location is rejected

- GIVEN a policy enables an injection location outside the exact supported set
- WHEN the launcher starts
- THEN it SHALL fail with the line number

##### Scenario: Inline value and source are rejected

- GIVEN a policy contains a native inline value or environment-source field
- WHEN the launcher starts
- THEN it SHALL reject the unsupported field before runtime configuration exists

##### Scenario: Name sets must match exactly

- GIVEN a name is present in only one source or differs only by letter case
- WHEN the launcher starts
- THEN it SHALL fail before sandbox creation
- AND it SHALL NOT resolve any missing value from the ambient environment

#### Requirement: Compatible secret runtime

Before ordinary environment processing, the launcher SHALL resolve `msb` through the initial host `PATH` to an external executable and canonicalize it. It SHALL reject that executable when it is lexically or physically inside, or hard-linked anywhere into, the projects root, workspace, repository build context, or another recursively exposed or project-controlled source tree. Only after this location and identity preflight SHALL the initial host `PATH` be trusted for runtime selection. The launcher SHALL record the accepted executable's filesystem identity in state that trusted ordinary configuration cannot change and use that absolute executable for compatibility and launch operations. A present pair SHALL require a successful absolute-executable `--version` process that writes nothing to stderr and writes to stdout exactly `msb MAJOR.MINOR.PATCH`, followed by either no terminator or one LF. Each component SHALL be decimal with no leading zero except zero itself. Nonzero status, stderr output, CRLF, trailing blank lines, prerelease suffixes, build metadata, extra text, and malformed output SHALL be rejected. Compatible versions SHALL be greater than or equal to 0.6.12 and less than 1.0.0. The launcher SHALL check compatibility after source and exposure preflight but before content parsing or real-value retention, so incompatibility SHALL take precedence over empty or malformed content. No-pair and reset flows SHALL NOT apply this feature-specific gate. After ordinary configuration, the launcher SHALL verify that the saved absolute path still has the checked identity and SHALL invoke that exact executable; changing `PATH` SHALL NOT select another runtime.

##### Scenario: Lower compatible boundary succeeds

- GIVEN a present pair and successful version output exactly `msb 0.6.12` with zero or one LF terminator and empty stderr
- WHEN the launcher checks compatibility
- THEN it SHALL proceed to protected configuration parsing

##### Scenario: Newer compatible versions succeed

- GIVEN a present pair and an exact successful patch or minor version newer than 0.6.12 but below 1.0.0
- WHEN the launcher checks compatibility
- THEN it SHALL proceed to protected configuration parsing

##### Scenario: Older version fails closed

- GIVEN a present pair and an exact successful version older than 0.6.12
- WHEN the launcher checks compatibility
- THEN it SHALL fail before parsing or exporting real values

##### Scenario: Future major fails pending review

- GIVEN a present pair and a successful version 1.0.0 or newer
- WHEN the launcher checks compatibility
- THEN it SHALL fail before parsing or exporting real values

##### Scenario: Ambiguous version fails closed

- GIVEN a present pair and a nonzero version process, stderr output, CRLF, trailing blank line, missing output, leading-zero component, prerelease or build suffix, extra text, or malformed output
- WHEN the launcher checks compatibility
- THEN it SHALL fail before parsing or retaining real values

##### Scenario: Project-controlled runtime is rejected

- GIVEN an initial `PATH` resolves `msb` to an executable inside or hard-linked into a project-controlled, built, mounted, or snapshotted source tree
- WHEN a present pair triggers compatibility checking
- THEN the launcher SHALL reject the executable before retaining project values

##### Scenario: Environment PATH change cannot replace runtime

- GIVEN a present pair and trusted ordinary configuration changes `PATH` to contain another `msb`
- WHEN the launcher invokes the runtime
- THEN it SHALL revalidate and invoke the absolute executable that passed compatibility checking

##### Scenario: Checked executable replacement fails closed

- GIVEN the checked absolute executable changes filesystem identity before final invocation
- WHEN the launcher prepares to retain or export real values
- THEN it SHALL fail without invoking the replacement

##### Scenario: No-secret launch remains compatible

- GIVEN no present pair
- WHEN the launcher starts with an incompatible runtime
- THEN the project-secret compatibility requirement SHALL NOT prevent launch

#### Requirement: Collision-free source references

`TAU_ENV_FILE` SHALL remain trusted executable host configuration. Intentional reads, output, traps, or other actions that it performs SHALL be outside the project-secret non-disclosure boundary. Before real project values are retained, the launcher SHALL disable xtrace, clear DEBUG, RETURN, ERR, and EXIT traps and related tracing state, and isolate subsequent secret handling from functions or instrumentation established by the sourced file.

After ordinary environment processing, the launcher SHALL allocate host-only source names from `TAU_SANDBOX_SECRET_SOURCE_<n>`. It SHALL skip any candidate present in the process environment, ordinary guest-environment names, project guest-secret names, or earlier allocations. Original guest secret names and existing host variables SHALL remain unchanged. Project values SHALL be exported under allocated names only in the final runtime subprocess, and allocated source names SHALL NOT be forwarded to the guest.

The generated scoped policy SHALL contain only double-quoted guest-name mapping keys, exact host environment references, validated destinations, and validated injection locations. Quoted keys SHALL deserialize as exact strings even when a name resembles a YAML implicit scalar. It SHALL have mode `0600` in a mode-`0700` directory under host `/tmp`, outside and non-aliased with every build or guest source. Cleanup SHALL remove it on every exit. Real values SHALL NOT enter the file or command arguments.

For compatible runtime versions, an exact environment reference SHALL resolve the source value literally at spawn without recursive expansion or durable plaintext storage; only the generated placeholder SHALL become the guest secret value. The host source variable SHALL NOT become a guest variable. Runtime-owned secret diagnostics and representations SHALL follow the compatible runtime's documented redaction contract; launcher-authored errors SHALL NOT include values.

##### Scenario: Occupied source candidates are skipped

- GIVEN the initial and one or more later source candidates are occupied by ambient, ordinary, project, or earlier allocated names
- WHEN the launcher allocates sources
- THEN it SHALL choose the first unoccupied candidate for each secret without modifying occupied variables

##### Scenario: Sources exist only in final subprocess

- GIVEN valid project values and allocated source names
- WHEN the launcher invokes the runtime
- THEN only the final runtime subprocess SHALL receive those source values
- AND the guest SHALL NOT receive the source names

##### Scenario: Generated schema contains no values

- GIVEN an active secret
- WHEN the launcher generates scoped policy
- THEN it SHALL emit a double-quoted guest-name key, an exact host source reference, destinations, and injection locations
- AND it SHALL NOT emit the real value

##### Scenario: Implicit-scalar-shaped name stays a string

- GIVEN a valid guest name is `true`, `false`, or `null`
- WHEN the launcher generates and the runtime parses scoped policy
- THEN the mapping key SHALL remain that exact string

##### Scenario: Temporary policy is private and cleaned

- GIVEN generated runtime policy is required
- WHEN launch succeeds or fails
- THEN its directory and file SHALL have private modes while present
- AND cleanup SHALL remove the generated directory

##### Scenario: Literal source text is not launcher-expanded

- GIVEN a real value contains text shaped like an environment reference
- WHEN the launcher exports it under the synthetic source name
- THEN the source process environment SHALL contain that exact literal text
- AND the launcher SHALL NOT resolve it as another environment reference

#### Requirement: Protected secret boundary

For each active secret, the launcher SHALL configure the compatible runtime to expose a generated guest placeholder instead of a real value and to permit substitution only for the validated HTTP(S) destinations and request locations in that secret's policy. The compatible runtime's documented contract SHALL govern DNS observation, TLS identity, HTTP authority, substitution, and violation handling. The launcher SHALL encode exactly the validated effective destinations and request locations after applying the documented omitted-`inject` default, and the destination allowlist SHALL NOT expand sandbox network policy.

##### Scenario: Guest receives placeholder only

- GIVEN an active secret named `OPENAI_API_KEY`
- WHEN a guest process reads that variable
- THEN it SHALL observe a runtime placeholder
- AND it SHALL NOT observe the real value injected by the launcher

##### Scenario: Host source is absent from guest

- GIVEN an active secret uses an allocated host source
- WHEN the guest inspects its environment
- THEN the host source name and value SHALL be absent

##### Scenario: Allowed request policy is encoded

- GIVEN an active secret permits an HTTPS destination and request location
- WHEN the launcher generates the compatible runtime policy
- THEN that destination and request location SHALL be present for the secret

##### Scenario: Disallowed effective policy remains absent

- GIVEN a destination or request location is absent from the validated effective policy after defaults
- WHEN the launcher generates the compatible runtime policy
- THEN it SHALL remain absent from the secret's runtime policy

##### Scenario: Secret policy adds no private egress rule

- GIVEN an active policy permits a private destination and no independent private-network exception is configured
- WHEN the launcher constructs the runtime invocation
- THEN it SHALL NOT add a network exception for that secret destination
- AND the existing sandbox network policy SHALL remain unchanged

#### Requirement: Environment source isolation

Before sourcing `TAU_ENV_FILE`, the launcher SHALL reject it when its normalized lexical path, resolved physical path, or filesystem identity matches the value source belonging to the present pair. The check SHALL include hard links and SHALL fail closed when identity cannot be established. Concurrent trusted-host mutation after this preflight SHALL be outside the supported threat model.

Project secret names and policy SHALL be validated without retaining real project values before ordinary forwarding. A matching `TAU_ENV_FILE` name SHALL be omitted from raw guest `KEY=value` arguments regardless of source order. After trusted ordinary configuration returns, subsequent value parsing and runtime preparation SHALL be isolated from its xtrace, traps, functions, and tracing state. Project source or runtime-preparation errors SHALL identify configuration without printing names, values, or secret-bearing lines.

##### Scenario: Same lexical environment source is rejected

- GIVEN `TAU_ENV_FILE` names the present-pair value source lexically
- WHEN the launcher starts
- THEN it SHALL fail before sourcing the file

##### Scenario: Resolved or hard-link environment alias is rejected

- GIVEN `TAU_ENV_FILE` resolves to or is a hard link of the present-pair value source
- WHEN the launcher starts
- THEN it SHALL fail before sourcing the file

##### Scenario: Command substitution cannot execute through alias

- GIVEN the aliased present-pair value source contains command-substitution syntax
- WHEN the launcher rejects the collision
- THEN it SHALL NOT execute that syntax

##### Scenario: Secret name suppresses raw forwarding

- GIVEN the same name occurs in the ordinary and project value sources
- WHEN the launcher constructs guest environment arguments
- THEN it SHALL omit the raw ordinary value for that name

##### Scenario: Trusted environment instrumentation is cleared

- GIVEN trusted ordinary configuration enables xtrace, traps, or replaceable shell functions
- WHEN the launcher subsequently retains and exports project values
- THEN those values SHALL NOT be printed or inspected by the inherited instrumentation

##### Scenario: Intentional trusted output is outside boundary

- GIVEN trusted ordinary configuration intentionally reads and prints a host secret source
- WHEN that trusted code executes
- THEN its intentional behavior SHALL be outside the launcher's project-secret non-disclosure guarantee

### MODIFIED Requirements

#### Requirement: Environment forwarding

Variables named in `TAU_ENV_FILE` (default `${HOME}/.env`) SHALL be forwarded as guest `KEY=value` arguments except when the name is an active project secret. Ordinary and project values SHALL NOT be baked into the image or printed by the launcher.

The launcher SHALL NOT causally place real project values in guest environment arguments, launcher diagnostics, generated policy data, image inputs, mounts, snapshots, or guest files. This SHALL NOT claim that matching bytes cannot pre-exist independently or that an explicitly allowed remote service cannot reflect a substituted value in its response.

##### Scenario: Ordinary variable remains forwarded

- GIVEN an ordinary environment variable is not a project secret
- WHEN the sandbox starts
- THEN the guest SHALL receive its real value through ordinary forwarding

##### Scenario: Project secret overrides ordinary forwarding

- GIVEN the same name exists in ordinary and active project sources
- WHEN the sandbox starts
- THEN the launcher SHALL NOT pass the ordinary value as a raw guest argument
- AND protected activation SHALL use the project value source

##### Scenario: Launcher does not place project value in guest data

- GIVEN an active value does not independently pre-exist in exposed data
- WHEN the launcher builds or starts a sandbox
- THEN it SHALL NOT place that value in output, arguments, image inputs, generated policy, snapshots, mounts, guest files, or raw guest environment

##### Scenario: Allowed reflection is outside causal guarantee

- GIVEN an allowed service reflects a substituted non-sensitive test value
- WHEN guest code prints or writes the response
- THEN the launcher's causal non-disclosure requirement SHALL remain satisfied

##### Scenario: Ordinary values remain undisclosed by launcher

- GIVEN an ordinary value is selected for forwarding
- WHEN the launcher builds or starts a sandbox
- THEN it SHALL NOT print the value or include it in image inputs

## Domain: Documentation

### ADDED Requirements

#### Requirement: Project secret documentation

User-facing documentation SHALL describe `TAU_PROJECTS_DIR`, exact host mapping, no inheritance, source grammars, reserved names, the compatibility gate triggered by a present pair, placeholders, destination restrictions, TLS identity requirements, request locations, ordinary-forwarding precedence, reset behavior, and network independence. It SHALL stop recommending raw ordinary forwarding for protected API keys and SHALL update all affected configuration, environment, security, testing, and prerequisite sections.

The sandbox environment reference SHALL distinguish ordinary forwarded values from protected placeholders and SHALL NOT imply that `env` reveals protected real values.

##### Scenario: User can configure protected API credentials

- GIVEN a user has a compatible runtime and an API credential with allowed destinations
- WHEN the user follows the documentation for an exact launch directory
- THEN it SHALL provide enough information to create valid paired sources and launch with placeholders

##### Scenario: User understands compatibility

- GIVEN a user has no present pair or has an incompatible runtime
- WHEN the user reads prerequisites and configuration
- THEN it SHALL explain that a present pair triggers the bounded version requirement and no pair skips it

##### Scenario: Guest understands the boundary

- GIVEN a sandbox starts with active project secrets
- WHEN a guest user reads the environment reference
- THEN it SHALL explain placeholders and policy-allowed substitution
- AND it SHALL NOT imply the real values are inspectable in the guest
