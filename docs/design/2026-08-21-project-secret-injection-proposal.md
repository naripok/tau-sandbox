# Proposal: Project Secret Injection

## Intent

Provide HTTP(S) API credentials to a sandbox according to the exact host directory from which it is launched, without mounting credential files or exposing real values in the guest environment. Host configuration remains outside the projects tree, and each credential is usable only for explicitly allowed destinations through Microsandbox secret substitution.

## Scope

**In scope:**

- Map launch directories under one configurable projects root to mirrored host-only directories under the user's home.
- Use paired `secrets.env` and strict `secrets.yaml` files for each exact launch directory.
- Parse a deliberately small, fully specified policy grammar and generate native Microsandbox scoped secret configuration.
- Require a compatible Microsandbox release whenever an exact present pair exists and fail closed without plaintext forwarding otherwise; no pair skips the feature gate.
- Load printable ASCII values as literal data into collision-free, host-only source variables without evaluating the value file as shell code.
- Require exact, case-sensitive agreement between value and policy names; reject reserved names, inline values, alternate source references, duplicates, and unsupported policy syntax.
- Prevent project secret names from also being forwarded as real guest environment values.
- Reject incomplete, malformed, non-regular, unreadable, colliding, project-tree-resident, build-exposed, or guest-exposable secret configuration before image work.
- Preserve reset and no-secret launch behavior without unnecessary secret processing or version gating.
- Document placeholders, destination allowlists, TLS substitution, request locations, and network-policy boundaries.

**Out of scope:**

- Credentials that must be plaintext in the guest, including SSH private keys, signing keys, request-signing secrets, and non-HTTP database credentials.
- Parent-directory inheritance or merging multiple project secret configurations.
- General YAML: inline collections, anchors, aliases, tags, escapes, inline comments, arbitrary fields, inline secret values, or alternate source references.
- IP-literal, port-qualified, internationalized, single-label, or any-host destination policies; allowed destinations are narrow ASCII DNS names and suffix wildcards.
- Secret request-body substitution; supported locations are `headers`, `basic_auth`, and `query_params`.
- Secret-manager integrations, credential creation, rotation, or editing commands.
- Project-controlled secret manifests or approval workflows.
- Expanding network access from a secret destination allowlist.
- Multiple projects roots or disambiguation of equal relative paths under different roots.
- Concurrent trusted-host mutation of configuration after preflight and before use. The sandbox cannot mutate these host-only sources; defending against a separately compromised host account is outside the isolation boundary.
- Intentional file reads, output, traps, or other actions performed by `TAU_ENV_FILE`, which remains trusted executable host configuration under the existing forwarding contract. The launcher isolates subsequent secret handling from shell instrumentation but cannot prevent trusted code from deliberately opening host files itself.
- Reimplementing Microsandbox's DNS, TLS, HTTP identity, placeholder, source-resolution, or diagnostic-redaction internals. The feature uses a bounded compatible release range whose documented contract supplies those guarantees.

## Approach

### Exact host mapping

`TAU_PROJECTS_DIR` selects the projects root and defaults to `$HOME/Projects`. An explicitly set empty value is invalid. Lexical paths are made absolute and normalized without following symlinks; physical paths additionally resolve symlinks. Containment uses complete path components, never string prefixes, and is checked in both forms. Physical location is authoritative for whether a launch belongs to the projects root: a lexical path outside that resolves inside is eligible, while a lexical path inside that resolves outside is not. Lexical checks remain independently authoritative for rejecting secret sources placed through misleading paths. If the launcher cannot establish identity or containment, it fails closed.

A launch directory that physically resolves to a proper descendant maps relative to the physical projects root under `$HOME`, with a dot prefixed to the first component:

| Launch directory | Secret directory |
| --- | --- |
| `$HOME/Projects/megali` | `$HOME/.megali` |
| `$HOME/Projects/megali/main` | `$HOME/.megali/main` |
| `$HOME/Projects/megali/main/api` | `$HOME/.megali/main/api` |

The mapping is exact; nested launches never inherit parent secrets. The projects root itself and launches outside it receive no selected project secrets.

When the default projects root is absent, discovery is disabled. A default or explicit root that has a directory entry but is dangling, non-directory, unreadable, or unsearchable is an error. A relative explicit root is resolved from the launch directory.

### Source validity and processing order

Reset remains the first project-specific action and never discovers secrets. For a normal launch, secret discovery and all exposure checks occur after basic path derivation but before image lookup, Podman build, environment-file sourcing, Tau snapshot creation, or mount construction.

A derived secret directory that has no directory entry means no project secrets. An existing entry must resolve to a readable and searchable directory. Within it, both `secrets.env` and `secrets.yaml` must be readable regular files, or both must be absent. Dangling symlinks and special files count as present and invalid rather than absent. Two present structurally valid files form a **present pair**, which triggers the runtime compatibility check before their content is parsed. A **validated configuration** is a present pair whose grammars and exact name sets pass; it must declare at least one secret. **Active secrets** are the validated entries supplied to Microsandbox. Empty or comment-only pairs are rejected after the compatibility gate.

Neither the directory nor either source may be lexically or physically inside the projects root. The source files also must not be aliases of files exposed through the workspace, host Tau config, shared credentials, `.agents`, immutable prompt, repository build context, or another direct/snapshotted host source. Before any image build or guest-source snapshot, the launcher scans exposed source trees for hard-link identity with the two files and scans recursively dereferenced Tau config links for overlap with the secret directory or files. Containment is checked in both directions. An inability to inspect a potentially exposed entry fails closed.

### Literal value format

`secrets.env` contains printable ASCII `NAME=VALUE` entries:

```dotenv
OPENAI_API_KEY=sk-...
GITHUB_TOKEN=github_pat_...
```

The parser:

- rejects NUL, control, and non-ASCII bytes before storing values, except CR/LF line endings;
- accepts LF or CRLF and strips only the CR belonging to CRLF;
- accepts a final assignment without a newline;
- ignores empty lines, space-only lines, and full-line comments whose first non-space character is `#`;
- forbids tabs;
- requires names in column one matching `[A-Za-z_][A-Za-z0-9_]*`;
- preserves every character after the first `=`, including spaces, quotes, `#`, `$`, and additional equals signs;
- accepts whitespace-only values but rejects zero-length values and duplicate names; and
- reports only the file, line number, and error class—not the name, line contents, or value.

The file is parsed as data and is never sourced. The following guest names are rejected because the image entrypoint or process startup owns them:

```text
HOME SHELL TERM COLORTERM USER LOGNAME PATH
PYTHONUSERBASE NPM_CONFIG_PREFIX PIP_USER TAU_NO_UPDATE_CHECK
TAU_SANDBOX_SHARED_CREDENTIALS BASH_ENV ENV IFS CDPATH GLOBIGNORE
SHELLOPTS BASHOPTS LD_PRELOAD LD_LIBRARY_PATH PYTHONHOME PYTHONPATH
NODE_OPTIONS
```

Names beginning `TAU_SANDBOX_SECRET_SOURCE_`, `TAU_ENTRYPOINT_`, or `BASH` are reserved. The image entrypoint will rename every internal shell variable under `TAU_ENTRYPOINT_`, making one enforceable namespace cover its current and future scratch state. The additional exact Bash-reserved names are:

```text
PWD OLDPWD SHLVL _ EUID UID PPID BASHPID LINENO FUNCNAME GROUPS
DIRSTACK PIPESTATUS RANDOM SECONDS HOSTNAME HOSTTYPE MACHTYPE OSTYPE
OPTERR OPTIND OPTARG PS1 PS2 PS4 EPOCHSECONDS EPOCHREALTIME SRANDOM
REPLY MAPFILE COPROC HISTCMD COLUMNS LINES
```

These names are assigned or interpreted by Bash even when they are not explicitly set by the image entrypoint.

### Strict policy grammar

`secrets.yaml` is ASCII and line-oriented. It is intentionally a strict YAML subset:

```yaml
OPENAI_API_KEY:
  allow:
    - api.openai.com

GITHUB_TOKEN:
  allow:
    - api.github.com
    - "*.githubusercontent.com"
  inject:
    - headers
    - query_params
```

Complete grammar:

1. Input uses LF or CRLF; a CR is removed only as part of CRLF. A final grammar line without a line terminator is accepted. NUL, BOM, non-ASCII bytes, and tabs are rejected.
2. Empty lines, space-only lines, and full-line comments beginning after zero or more spaces may appear between grammar elements and do not change parser state. Inline comments are forbidden.
3. A secret header is exactly `NAME:` in column one, with no trailing spaces. `NAME` follows the value-file grammar and reserved-name rules.
4. The first field is exactly two spaces followed by `allow:`. It occurs once.
5. One or more allow items immediately follow, each exactly four spaces, `- `, and one scalar. After blank/full-comment lines are ignored, another field or secret header ends the list.
6. The optional second field is exactly two spaces followed by `inject:`. It occurs at most once and only after `allow`. When omitted, the validated effective policy enables `headers` and no other request location.
7. One or more injection items immediately follow in the same four-space list form. Values are exactly `headers`, `basic_auth`, or `query_params`.
8. No other indentation, trailing whitespace, field order, field, or syntax is accepted.
9. An allow scalar is either unquoted or enclosed by one matching pair of single or double quotes. Quotes contain no escapes and must contain the whole scalar. After unquoting, the value must be an ASCII DNS name or `*.` suffix wildcard. Exact names contain two or more dot-separated labels; wildcard suffixes contain two or more labels after `*.`. Each label is 1–63 characters, begins and ends with an ASCII letter or digit, and contains only letters, digits, and internal hyphens. An exact name is at most 253 characters. A wildcard suffix is at most 253 characters and the complete `*.` scalar is therefore at most 255 characters. Values are canonicalized to lowercase before duplicate comparison and generated output. `*`, IP literals, ports, empty scalars, interpolation markers, and every other character are rejected.
10. Secret names, fields, and injection values compare case-sensitively. Duplicate names, fields, canonical destinations, and injection values are rejected.
11. The value and policy name sets must match exactly and contain at least one name.

This grammar makes arbitrary native `value:` and source fields unrepresentable and ensures every generated scalar can be serialized without interpreting user syntax.

### Generated native configuration

`TAU_ENV_FILE` remains trusted executable host configuration for compatibility. Before sourcing it, the launcher validates project names and policy without retaining real project values. After sourcing, it disables xtrace, resets DEBUG/RETURN/ERR/EXIT traps and related tracing state, and runs final value parsing, source allocation, generated-policy creation, and export inside a clean subshell whose secret-handling functions cannot be replaced by the sourced file. Intentional reads or output performed by the trusted file itself are outside the feature boundary; subsequent project-secret handling is not exposed to instrumentation it leaves behind.

The clean subshell allocates one host source name per secret after ordinary processing. Candidates use `TAU_SANDBOX_SECRET_SOURCE_<n>` and skip every candidate present in the complete process environment, ordinary guest-environment names, project guest-secret names, or already allocated sources. Project values are exported under synthetic names only in that final subshell, which immediately `exec`s `msb`; the parent launcher and original guest secret names are not modified.

Every generated guest mapping key is double-quoted, including names such as `true`, `false`, and `null`, so strict YAML deserialization preserves it as the exact environment-variable string. The generated scoped document has mode `0600` inside a mode-`0700` directory created under fixed host `/tmp`. Its path and physical directory are checked against all guest and build sources before use, explicitly added to cleanup, and never mounted. Validated input produces only this schema:

```yaml
"OPENAI_API_KEY":
  value: "${TAU_SANDBOX_SECRET_SOURCE_0}"
  allow:
    - "api.openai.com"
  inject:
    - headers
```

Real values never enter the file or command arguments. In Microsandbox 0.6.12, an exact `${NAME}` in a secret `value` records a host environment source reference, resolves that variable literally at spawn without recursive expansion, stores no plaintext in durable config, exposes only the generated guest placeholder, and does not forward the source variable to the guest. The runtime's documented secret handling and redacted secret representation are part of the pinned compatibility contract; the launcher does not attempt to filter arbitrary child-process output.

### Runtime compatibility

Before ordinary environment processing, the launcher resolves `msb` through the initial host `PATH` to an external executable, canonicalizes it, and rejects it if it is lexically or physically inside, or hard-linked anywhere into, the projects root, workspace, repository build context, or any other recursively exposed/project-controlled source tree. The initial `PATH` is trusted host input only after this executable-location preflight. The launcher records the accepted executable's filesystem identity in readonly state and uses that absolute executable for the version gate. A present pair triggers that executable's `--version`. The command must exit zero, write nothing to stderr, and write to stdout exactly `msb MAJOR.MINOR.PATCH` with either no terminator or one terminating LF. Each component is decimal without leading zeroes except zero itself. Prerelease suffixes, build metadata, additional text, CRLF, trailing blank lines, and malformed output are rejected. Compatible versions are `>=0.6.12` and `<1.0.0`; a future major release requires explicit review before acceptance. File/exposure preflight precedes this command; grammar parsing and real-value retention follow it, so an incompatible runtime takes precedence over empty or malformed content.

The version check occurs after present-pair source/exposure preflight but before parsing or exporting real values. After trusted ordinary configuration runs, the launcher verifies that the saved absolute path still identifies the checked executable and invokes that exact path, so a `PATH` change cannot redirect secrets to another program. Missing, replaced, older, future-major, prerelease, or unparseable runtimes fail closed. No-secret and reset flows do not apply this feature-specific gate. `install.sh` remains unchanged because installation is not associated with an exact project secret directory; README documents that a present pair triggers the compatibility range.

### Environment and exposure controls

Secret names are validated before ordinary forwarding, so a matching name from `TAU_ENV_FILE` is omitted from raw `-e` arguments. Before sourcing the ordinary file, the launcher rejects lexical-path, resolved-path, and hard-link identity with `secrets.env`. Concurrent trusted-host mutation after this check is outside scope. The sourced file is trusted executable configuration; launcher guarantees begin after its intentional behavior and include clearing inherited shell tracing/traps before any real project value is retained.

The launcher's causal non-disclosure contract is that it never places real project values in guest environment arguments, its diagnostics, generated config, image build inputs, mounts, snapshots, or guest files. This does not claim matching bytes cannot pre-exist independently in exposed data or that an explicitly allowed service cannot reflect a substituted test credential in a response.

For compatible Microsandbox releases, the guest receives a placeholder. Microsandbox substitutes the value only for policy-allowed HTTP(S) request locations satisfying its DNS observation, TLS identity, and HTTP authority checks. Secret destinations do not grant network access; existing `--net` and `TAU_LAN_HOSTS` policy remains authoritative.

### Verification strategy

Unit tests with fake `msb`/Podman cover ordering, mapping, roots, directory/file states, grammar boundaries, NUL/control/non-ASCII rejection, reserved names, exact name sets, source allocation collisions, generated schema/modes/cleanup, semantic version boundaries, raw-forwarding suppression, identity/containment/hard-link checks, safe diagnostics, and absence of values from arguments/files/logs.

Real-runtime integration tests require a compatible installed `msb` and verify guest placeholders, absence of synthetic source variables in the guest, and literal special-character values remaining host-side. Deterministic end-to-end TLS substitution would require a separately reachable HTTPS fixture and CA/routing setup that this repository does not have; destination, request-location, DNS, TLS, authority, source-resolution, and runtime-redaction internals remain delegated to the bounded Microsandbox compatibility contract rather than tested against a third-party endpoint.

## Impact

- `run.sh` gains early secret preflight, exact mapping, portable identity/collision checks, strict value/policy parsers, reserved-name validation, exact name-set validation, collision-free source allocation, absolute runtime identity/version checking, generated `--secret-conf` input, raw-forwarding suppression, secure temporary-file cleanup, and safe diagnostics.
- `config/entrypoint.sh` moves all internal shell variables under the reserved `TAU_ENTRYPOINT_` prefix so guest secret placeholders cannot be overwritten by entrypoint scratch assignments.
- `README.md` gains `TAU_PROJECTS_DIR`, file grammars, exact nested semantics, the present-pair compatibility range, examples, migration away from raw API-key forwarding, request/network boundaries, and updated configuration/security/testing/prerequisite sections.
- `docs/SPEC.md` gains accepted behavioral requirements after implementation.
- `config/APPEND_SYSTEM.md` distinguishes protected placeholders from ordinary forwarded values and no longer implies protected real values are inspectable with `env`.
- `install.sh` behavior does not change.
- Tests gain the unit and compatible-runtime coverage described above without adding a host runtime dependency.
