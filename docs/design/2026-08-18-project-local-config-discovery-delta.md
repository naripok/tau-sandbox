# Delta: Project-Local Tau Config Discovery

## Domain: sandbox-launch

### ADDED Requirements

#### Requirement: Project-local config discovery
When `TAU_CONFIG_DIR` is unset, `run.sh` SHALL select as the host Tau config directory the `.tau` entry of the nearest ancestor of the launch directory, where the launch directory itself counts as an ancestor and `.tau` entries are matched as directories with symlinks followed; a dangling `.tau` symlink SHALL NOT match. When no ancestor matches, `${HOME}/.tau` SHALL apply. An explicitly set `TAU_CONFIG_DIR` SHALL take precedence over discovery, and discovery SHALL NOT change the resolved project path used for volume derivation.

##### Scenario: Nearest project root config is discovered
- GIVEN `TAU_CONFIG_DIR` is unset, `<project-root>/.tau` is a directory, and `run.sh` launches from `<project-root>/nested/dir`
- WHEN the launch proceeds
- THEN `<project-root>/.tau` SHALL be the host Tau config directory

##### Scenario: Innermost config wins
- GIVEN `.tau` directories at both `<project-root>` and `<project-root>/nested`, and `run.sh` launches from `<project-root>/nested/dir`
- WHEN the launch proceeds
- THEN `<project-root>/nested/.tau` SHALL be the host Tau config directory

##### Scenario: Discovered config via root symlink
- GIVEN `<project-root>/.tau` is a symlink to directory `<config-world>` and `TAU_CONFIG_DIR` is unset
- WHEN `run.sh` launches from a directory under `<project-root>`
- THEN `<config-world>` SHALL be the host Tau config directory

##### Scenario: No discovery match falls back to the default
- GIVEN no ancestor's `.tau` entry is a directory (absent or a dangling symlink) and `TAU_CONFIG_DIR` is unset
- WHEN `run.sh` launches
- THEN `${HOME}/.tau` SHALL be the host Tau config directory

##### Scenario: Explicit override beats discovery
- GIVEN `<project-root>/.tau` is a directory and `TAU_CONFIG_DIR` is set to `<override-dir>`
- WHEN `run.sh` launches from a directory under `<project-root>`
- THEN `<override-dir>` SHALL be the host Tau config directory
