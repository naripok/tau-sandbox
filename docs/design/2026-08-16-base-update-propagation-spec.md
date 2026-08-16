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
  SHA-256 digests of the image build-context files — the repository
  `Containerfile` and every file directly under `config/` — concatenated in the
  filesystem sort order of their paths. It SHALL change when the content of any
  of those files changes and SHALL be stable when none does.

The launcher SHALL require interactive approval before building a missing
package-specific image. If the base-hash inputs cannot be computed (a file is
missing or `config/` contains a non-file entry), the launcher SHALL abort with an
error rather than launch with an image whose freshness cannot be determined.

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

##### Scenario: Uncomputable base hash aborts the launch

- GIVEN `config/` contains a directory or a build-context file is missing
- WHEN the project launches
- THEN the launcher SHALL abort with an error and SHALL NOT build or boot an
  image

### ADDED Requirements

#### Requirement: Superseded package images are pruned

When the launcher builds a package-specific image, it SHALL remove from the
microsandbox cache every other image whose reference matches
`localhost/tau-agent-isolated-<basename>-<8 hex>[:<8 hex>]:latest` (both the
current two-hash form and the legacy single-hash form). It SHALL NOT remove the
image it just loaded, and SHALL ignore images that are already absent.

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
