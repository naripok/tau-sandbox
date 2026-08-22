# Spec: Project-Secret Simplification

## Domain: project-secrets

### ADDED Requirements

#### Requirement: Paired secret sources

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

##### Scenario: Absent directory disables secrets

- GIVEN the exact derived secret directory has no directory entry
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

##### Scenario: Invalid derived directory fails

- GIVEN the derived secret directory entry is a dangling symlink, a regular
  file, or an unreadable or unsearchable directory
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the directory

##### Scenario: Absent pair disables secrets

- GIVEN the derived directory exists and neither exact source entry exists
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

##### Scenario: Incomplete pair fails

- GIVEN exactly one exact source entry exists
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the missing source

##### Scenario: Invalid source type fails

- GIVEN either exact source is a directory, device, socket, FIFO, unreadable
  file, or dangling symlink
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the invalid source

##### Scenario: Secret directory escaping projects root fails

- GIVEN the derived secret directory entry exists and is a symlink resolving
  inside the physical projects root, with or without a pair inside
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the directory

##### Scenario: Secret values win over ordinary assignments

- GIVEN the same name is assigned in `TAU_ENV_FILE` and declared in
  `secrets.env`
- WHEN the launcher sources both files in order
- THEN the runtime process environment SHALL contain the `secrets.env` value
  for that name

##### Scenario: Reserved name fails fast

- GIVEN `secrets.env` declares a reserved name such as `PATH` or `TAU_HOME`
- WHEN the launcher loads the pair
- THEN it SHALL fail with a message naming the offending variable
- AND the sandbox SHALL NOT be created

##### Scenario: Non-assignment lines declare no names

- GIVEN `secrets.env` contains comments, blank lines, or lines without a
  top-level assignment
- WHEN the launcher loads the pair
- THEN those lines SHALL declare no names for the reserved-name check and
  forwarding suppression

##### Scenario: Policy passes through unmodified

- GIVEN a present pair with any `secrets.yaml` content
- WHEN the launcher invokes the runtime
- THEN it SHALL pass that exact file path as `--secret-conf`
- AND it SHALL NOT parse, validate, or regenerate the policy

### MODIFIED Requirements

#### Requirement: Exact secret location mapping

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

##### Scenario: Project maps to hidden home location

- GIVEN the default projects root and a launch from its `megali` child
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali` location

##### Scenario: Nested launch maps exactly

- GIVEN the default projects root and a launch from its `megali/main/api`
  descendant
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

- GIVEN a lexical launch path outside the lexical projects root physically
  resolves to `megali/main` under the physical projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL select the user's `.megali/main` location

##### Scenario: Lexically inside launch resolves outside

- GIVEN a lexical launch path inside the lexical projects root physically
  resolves outside the physical projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL launch without automatically selected project secrets

##### Scenario: Root and outside launches have no secrets

- GIVEN the physical launch directory equals or is outside the physical
  projects root
- WHEN the launcher resolves project secrets
- THEN it SHALL launch without automatically selected project secrets

##### Scenario: Unusable default root disables discovery

- GIVEN `TAU_PROJECTS_DIR` is unset and `${HOME}/Projects` is absent,
  dangling, non-directory, unreadable, or unsearchable
- WHEN the launcher starts
- THEN it SHALL launch without project secrets

##### Scenario: Invalid explicit root fails

- GIVEN `TAU_PROJECTS_DIR` is set to an empty value or a path that is not a
  usable directory
- WHEN the launcher starts
- THEN it SHALL fail with a message identifying the setting

#### Requirement: Protected secret boundary

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

##### Scenario: Guest receives placeholder only

- GIVEN an active secret named `OPENAI_API_KEY`
- WHEN a guest process reads that variable
- THEN it SHALL observe a runtime placeholder
- AND it SHALL NOT observe the real value

##### Scenario: Exactly one secret-conf argument

- GIVEN a present pair
- WHEN the launcher builds the runtime invocation
- THEN exactly one `--secret-conf` argument naming the pair's `secrets.yaml`
  SHALL appear immediately after `run`
- AND every guest argument after the `--` separator SHALL be preserved
  byte-for-byte

##### Scenario: Launch without a pair passes no secret-conf

- GIVEN no present pair
- WHEN the launcher builds the runtime invocation
- THEN it SHALL pass no `--secret-conf` argument

##### Scenario: Secret policy adds no private egress rule

- GIVEN an active policy permits a private destination and no independent
  private-network exception is configured
- WHEN the launcher constructs the runtime invocation
- THEN it SHALL NOT add a network exception for that secret destination

#### Requirement: Reset bypasses secret discovery

A reset invocation SHALL remove the current project's persistent volumes
without locating, opening, sourcing, or validating project-secret
configuration. Secret errors SHALL NOT prevent reset.

##### Scenario: Invalid secrets do not block reset

- GIVEN the exact project secret location contains invalid or unreadable
  entries
- WHEN the user invokes reset
- THEN the launcher SHALL perform its existing volume-removal behavior
- AND it SHALL NOT read or validate either secret source

#### Requirement: Environment forwarding

Variables named in `TAU_ENV_FILE` (default `${HOME}/.env`) SHALL be forwarded
as guest `KEY=value` arguments except when the name is a declared project
secret; the raw ordinary value for such a name SHALL be omitted regardless of
source order. Ordinary and project values SHALL NOT be baked into the image
or printed by the launcher. Trusted host configuration that intentionally
reads or prints host secret sources is outside the launcher's non-disclosure
guarantee.

##### Scenario: Ordinary variable remains forwarded

- GIVEN an ordinary environment variable is not a declared project secret
- WHEN the sandbox starts
- THEN the guest SHALL receive its real value through ordinary forwarding

##### Scenario: Project secret suppresses raw forwarding

- GIVEN the same name is declared in `TAU_ENV_FILE` and in `secrets.env`,
  and the pair's policy covers that name
- WHEN the launcher constructs guest environment arguments
- THEN it SHALL omit the raw ordinary value for that name
- AND the protected placeholder SHALL be the only guest variable of that name

##### Scenario: Launcher does not print or bake values

- GIVEN ordinary or project values are selected for a launch
- WHEN the launcher builds or starts a sandbox
- THEN it SHALL NOT print those values or include them in image inputs

#### Requirement: Project secret documentation

User-facing documentation SHALL describe `TAU_PROJECTS_DIR`, the exact host
mapping, no inheritance, the paired-sources contract (sourced `secrets.env`,
runtime-native `secrets.yaml` passed through via `--secret-conf`), the
reserved-name set, ordinary-forwarding suppression, placeholders,
destination-scoped substitution as the runtime's contract, reset behavior,
and network independence. It SHALL stop recommending raw ordinary forwarding
for protected API keys. The sandbox environment reference SHALL distinguish
ordinary forwarded values from protected placeholders and SHALL NOT imply
that `env` reveals protected real values.

##### Scenario: User can configure protected API credentials

- GIVEN a user has a runtime supporting `--secret-conf` and an API credential
  with allowed destinations
- WHEN the user follows the documentation for an exact launch directory
- THEN it SHALL provide enough information to create valid paired sources and
  launch with placeholders

##### Scenario: Guest understands the boundary

- GIVEN a sandbox starts with active project secrets
- WHEN a guest user reads the environment reference
- THEN it SHALL explain placeholders and policy-allowed substitution
- AND it SHALL NOT imply the real values are inspectable in the guest

### REMOVED Requirements

#### Requirement: Early source preflight

Discovery and loading now happen after image build and immediately before
ordinary environment forwarding; there is no preflight ordering to specify.
Pair classification moves to "Paired secret sources"; content validation
(empty or malformed pair) is now the runtime's responsibility via
`--secret-conf`.

#### Requirement: Host-only source isolation

The exposure-descriptor registry, recursive traversals, and overlap and
hard-link detection are removed. Secrets live under `$HOME` outside the
projects root by construction; the single remaining placement check is the
physical symlink-escape rule in "Paired secret sources".

#### Requirement: Literal secret value grammar

`secrets.env` is sourced as trusted shell instead of parsed against a strict
grammar. Value format (quoting, spaces, `export` prefixes) follows shell
syntax; the launcher performs no per-line validation. The declared-name
definition and reserved-name set move to "Paired secret sources"; the
entrypoint namespacing invariant is retained there.

#### Requirement: Restricted secret policy grammar

`secrets.yaml` is passed to the runtime unmodified. The policy grammar is the
runtime's native `--secret-conf` format; the launcher no longer parses,
validates, or regenerates policy.

#### Requirement: Compatible secret runtime

No version check is performed. A runtime without `--secret-conf` support
fails naturally on the unknown flag. The reset requirement is updated to drop
its compatibility references (see MODIFIED "Reset bypasses secret
discovery").

#### Requirement: Collision-free source references

No synthetic host-source variables are allocated. The user's policy
references names from their own `secrets.env`, which the launcher exports
into the runtime's environment.

#### Requirement: Environment source isolation

`TAU_ENV_FILE` alias vetting and post-sourcing instrumentation isolation are
removed: both files are trusted host configuration on a single-user machine.
The forwarding-suppression behavior is retained in "Environment forwarding".
