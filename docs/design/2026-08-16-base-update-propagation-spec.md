# Spec: Base-Update Propagation to Per-Project Package Images

## Domain: sandbox-launch

Delta against `docs/SPEC.md`.

### MODIFIED Requirements

#### Requirement: Per-project package declarations

<!-- Replaces the image-name rule. The package hash rule is unchanged. -->

A non-empty `.tau-packages` file SHALL select image
`tau-agent-isolated-<basename>-<base-hash>-<package-hash>`, where:

- `<package-hash>` is derived from the raw bytes of `.tau-packages` (unchanged
  from the living spec);
- `<base-hash>` is the first eight hexadecimal characters of the SHA-256 of the
  text formed by concatenating, in lexicographic path order, the hex-encoded
  SHA-256 digests (64 lowercase hex characters, no separators) of the raw bytes
  of each regular file directly under `config/`, preceded by the digest of the
  repository `Containerfile`. It SHALL change when the content, or the set, of
  those files changes (a file added or removed) and SHALL be stable when none
  does. Non-regular entries under `config/` SHALL be ignored.

The launcher SHALL require interactive approval before building a missing
package-specific image. If the repository `Containerfile` or the `config/`
directory is missing, the launcher SHALL abort with an error rather than
derive a tag whose freshness cannot be verified against the current inputs.

##### Scenario: Base input change invalidates the package image

- GIVEN a project whose package image was built from an earlier build context
- WHEN the content of `Containerfile` or a `config/` file changes and the
  `.tau-packages` content does not
- THEN the launcher SHALL select a different image tag than the previously built
  one
- AND building it SHALL require the same interactive approval as any missing
  package image

##### Scenario: Unchanged inputs reuse the cached image

- GIVEN the package image tag derived from the current build context exists in
  the microsandbox cache
- WHEN the project launches and stdin is not a terminal
- THEN the launcher SHALL NOT build anything and SHALL boot the cached image

##### Scenario: Non-interactive base-triggered rebuild is refused

- GIVEN the package image tag is missing because the build context changed and
  stdin is not a terminal
- WHEN the project launches
- THEN the launcher SHALL fail without building

##### Scenario: Missing base inputs abort the launch

- GIVEN the repository `Containerfile` or the `config/` directory is missing
- WHEN the project launches
- THEN the launcher SHALL abort with an error and SHALL NOT build or boot an
  image

##### Scenario: Non-file config entries do not affect the hash

- GIVEN `config/` contains a directory alongside its regular files and the
  package image tag exists in the cache
- WHEN the project launches
- THEN the launcher SHALL select the same tag as it would without the directory
- AND it SHALL boot the cached image without building

### ADDED Requirements

#### Requirement: Superseded package images are pruned

When the launcher builds a package-specific image, it SHALL remove from the
microsandbox cache every other image whose reference matches
`localhost/tau-agent-isolated-<basename>-<8 hex>:latest` (legacy single-hash
form) or `localhost/tau-agent-isolated-<basename>-<8 hex>-<8 hex>:latest`
(current two-hash form). It SHALL NOT remove the image it just loaded. A failed
removal SHALL NOT fail the build, the load, or the launch, and SHALL NOT be
reported as an error.

##### Scenario: Base-triggered rebuild removes the superseded image

- GIVEN the cache contains a package image whose tag derives from an older base
  hash
- WHEN the launcher rebuilds the package image for the changed base
- THEN the old image SHALL be removed from the cache
- AND the newly built image SHALL remain

##### Scenario: Fresh build with an empty cache succeeds

- GIVEN the cache contains no package images for the project
- WHEN the launcher builds the package image for the first time
- THEN the build and load SHALL succeed and no removal error SHALL be reported

##### Scenario: Failed removal does not fail the launch

- GIVEN the cache contains a superseded package image whose removal from the
  cache fails
- WHEN the launcher rebuilds the package image for the changed base
- THEN the build, the load, and the launch SHALL succeed
- AND no removal error SHALL be reported
