# Delta: Base-Update Propagation to Per-Project Package Images

## Domain: sandbox-launch

### MODIFIED Requirements

#### Requirement: Per-project package declarations

For this requirement, a `.tau-packages` file is non-empty when it declares at
least one package name after stripping comments, blank lines, and surrounding
whitespace. A non-empty `.tau-packages` file SHALL select image
`tau-agent-isolated-<basename>-<base-hash>-<package-hash>`, where:

- `<package-hash>` is derived from the raw bytes of `.tau-packages` (unchanged
  behavior);
- `<base-hash>` is the first eight hexadecimal characters of the SHA-256 of the
  text formed by concatenating, in lexicographic path order, the hex-encoded
  SHA-256 digests (64 lowercase hex characters, no separators) of the raw bytes
  of each regular file directly under `config/` (including dotfiles), preceded
  by the digest of the repository `Containerfile`. It SHALL change when the
  content, or the set, of those files changes (a file added or removed) and
  SHALL be stable when none does. Non-regular entries under `config/` SHALL be
  ignored.

When the launcher would otherwise derive a package image tag (a non-empty
`.tau-packages` file is present and no `TAU_IMAGE` override is set), a missing
repository `Containerfile` or `config/` directory SHALL abort the launch with
an error rather than derive a tag whose freshness cannot be verified against
the current inputs.

##### Scenario: Base input change invalidates the package image

- GIVEN a project whose package image was built from an earlier build context
- WHEN the content of `Containerfile` or a `config/` file changes and the
  `.tau-packages` content does not
- THEN the launcher SHALL select a different image tag than the previously built
  one
- AND building it SHALL require the same interactive approval as any missing
  package image

##### Scenario: Added base input changes the tag

- GIVEN a package image was tagged from a base-input set without a particular
  regular file under `config/`
- WHEN that file is added to `config/` without changing any other input
- THEN the launcher SHALL select a different image tag than the previously
  built one

##### Scenario: Removed base input changes the tag

- GIVEN a package image was tagged from a base-input set that included a
  particular regular file under `config/`
- WHEN that file is removed without changing any other input
- THEN the launcher SHALL select a different image tag than the previously
  built one

##### Scenario: Non-file config entries do not affect the hash

- GIVEN `config/` contains a directory alongside its regular files and the
  package image tag exists in the cache
- WHEN the project launches
- THEN the launcher SHALL select the same tag as it would without the directory
- AND it SHALL boot the cached image without building

##### Scenario: Unchanged inputs reuse the cached image

- GIVEN the package image tag derived from the current build context exists in
  the microsandbox cache
- WHEN the project launches and stdin is not a terminal
- THEN the launcher SHALL NOT build anything
- AND the launcher SHALL NOT remove any image from the cache
- AND the launcher SHALL boot the cached image

##### Scenario: Non-interactive base-triggered rebuild is refused

- GIVEN the package image tag is missing because the build context changed and
  stdin is not a terminal
- WHEN the project launches
- THEN the launcher SHALL fail without building

##### Scenario: Missing base inputs abort a package-tag launch

- GIVEN the project has a non-empty `.tau-packages` file and no `TAU_IMAGE`
  override, and the repository `Containerfile` or the `config/` directory is
  missing
- WHEN the project launches
- THEN the launcher SHALL abort with an error and SHALL NOT build or boot an
  image

##### Scenario: Missing base inputs do not affect other launches

- GIVEN the repository `Containerfile` or the `config/` directory is missing
- WHEN a project without a non-empty `.tau-packages` file, or with a
  `TAU_IMAGE` override, launches
- THEN the launcher SHALL proceed with the shared base image or the override
  and SHALL NOT abort

### ADDED Requirements

#### Requirement: Superseded package images are pruned

Package image tags are keyed by basename, base hash, and package hash, so
same-basename projects share one tag namespace: projects with identical base
inputs and identical `.tau-packages` content use the same tag, while different
package contents produce different tags under the same basename prefix.

When the launcher builds a package-specific image, it SHALL remove from the
microsandbox cache every other image whose reference is
`localhost/tau-agent-isolated-<basename>-<package-hash>:latest` (legacy
single-hash form of the current package content) or
`localhost/tau-agent-isolated-<basename>-<8 hex>-<package-hash>:latest` (any
base version of the current package content). It SHALL NOT remove the image it
just loaded. Inherent to the shared tag namespace, an image carrying the
current package hash at another base hash is removed whether this project or a
same-basename project with identical `.tau-packages` content produced it.
Images tagged with any other package hash — including those of same-basename
projects with different `.tau-packages` content and those of earlier package
contents of this project — SHALL NOT be removed; in particular, a legacy
single-hash tag whose hash differs from the current package hash SHALL NOT be
removed. A failed removal SHALL NOT fail the build, the load, or the launch,
and SHALL NOT be reported as an error.

##### Scenario: Base-triggered rebuild removes the superseded image

- GIVEN the cache contains a package image for the current package content
  whose tag derives from an older base hash (two-hash form)
- WHEN the launcher rebuilds the package image for the changed base
- THEN the old image SHALL be removed from the cache
- AND the newly built image SHALL remain

##### Scenario: Legacy single-hash image is pruned

- GIVEN the cache contains a package image tagged in the legacy single-hash
  form for the same project and the same package content
- WHEN the launcher rebuilds the package image
- THEN the legacy image SHALL be removed from the cache
- AND the newly built image SHALL remain

##### Scenario: Legacy tag of another content is kept

- GIVEN the cache contains a package image tagged in the legacy single-hash
  form whose hash differs from the current package hash
- WHEN the launcher rebuilds the package image
- THEN that image SHALL remain in the cache

##### Scenario: Same-basename projects keep their package images

- GIVEN two projects with the same basename and different `.tau-packages`
  contents both have cached package images
- WHEN the launcher rebuilds the package image for one of them
- THEN the other project's image SHALL remain in the cache

##### Scenario: Earlier package content image survives a rebuild

- GIVEN the cache contains a package image tagged from earlier `.tau-packages`
  content of the same project
- WHEN the launcher rebuilds the package image for the current content
- THEN the earlier-content image SHALL remain in the cache

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
