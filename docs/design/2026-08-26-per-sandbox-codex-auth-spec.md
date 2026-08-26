# Spec: Per-Sandbox Codex OAuth

The living spec (`docs/SPEC.md`) has no `Domain:` sections; requirements live under a single `## Requirements` list. This delta names one domain, **Project Credentials**, and treats the existing `Writable shared credentials exception` requirement as its current content. Sharing one rotating refresh token across isolated sandboxes cannot keep every consumer valid, because only the refreshing process receives the successor token; per-project credentials replace that sharing.

## Domain: Project Credentials

### ADDED Requirements

#### Requirement: Project-local credential storage

The launcher SHALL NOT mount host `credentials.json` into the guest. Each sandbox SHALL read and write a credential file inside its project persistent home volume. Two sandboxes for different projects SHALL NOT share a credential file. Credential creation and update SHALL occur only in the project volume, never on the host.

##### Scenario: Launch does not mount host credentials

- GIVEN host `credentials.json` exists
- WHEN the launcher starts a sandbox for a project
- THEN the launcher SHALL NOT add a mount for the host credential file
- AND the guest credential path SHALL resolve inside the project home volume

##### Scenario: First-time credential stays in the project volume

- GIVEN a project with no stored credential
- WHEN a sandbox for that project creates a credential
- THEN the credential SHALL be written inside the project home volume
- AND the host config SHALL remain unchanged

##### Scenario: Two projects are isolated

- GIVEN sandboxes for two different projects
- WHEN each sandbox stores a credential
- THEN each credential SHALL persist only in its own project volume

#### Requirement: Host login helper produces a readable project credential

A host-side helper SHALL run the OpenAI Codex authorization flow on the host and SHALL produce a credential that a sandbox for the chosen project can load unchanged. The helper SHALL place the credential inside the project home volume. The helper SHALL NOT publish a guest port and SHALL NOT require network access into the guest.

##### Scenario: Browser login completes on the host

- GIVEN a project and a host with a browser
- WHEN the user runs the helper for the project
- THEN the helper SHALL complete the Codex authorization flow against the host callback
- AND the credential SHALL appear inside the project home volume
- AND a sandbox for that project SHALL load the credential unchanged

##### Scenario: Headless host falls back to paste

- GIVEN a host with no browser, or a host whose callback port is occupied
- WHEN the user runs the helper
- THEN the helper SHALL print the authorization URL and accept a pasted redirect URL
- AND a sandbox for that project SHALL load the resulting credential unchanged

#### Requirement: Concurrent refresh spends a rotating token once

When two processes share one project credential file, a refresh SHALL spend a rotating refresh token at most once. No process SHALL receive a refresh-token-reused error from concurrent refresh of one shared file.

##### Scenario: Concurrent refresh spends the token once

- GIVEN two processes share one project credential file with an expired token
- WHEN both processes begin a refresh before either refresh completes
- THEN exactly one refresh request SHALL use the stored refresh token
- AND the other process SHALL use the rotated credential written by the winner

### MODIFIED Requirements

#### Requirement: Writable shared credentials exception

This requirement previously required sharing the host credential file. It now requires the opposite: no sharing, and whole-file updates.

The launcher SHALL NOT mount host `credentials.json`. A credential update in a guest SHALL leave the project credential file whole: a concurrent reader SHALL observe either the old or the new complete credential, never a partial file.

##### Scenario: Credential update stays whole for readers

- GIVEN a sandbox with a project-local credential file
- WHEN Tau updates a stored credential while another process reads the file
- THEN the reader SHALL observe either the old or the new complete credential
- AND it SHALL NOT observe a partial file
