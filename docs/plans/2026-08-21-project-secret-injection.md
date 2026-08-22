# Project Secret Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject exact-directory HTTP(S) API credentials through Microsandbox placeholders while keeping real values and policy sources outside projects, images, mounts, and guest environments.

**Architecture:** A host-only Bash library owns exact discovery, source/executable identity, exposure preflight, strict source grammars, compatible-runtime policy generation, and isolated invocation. `run.sh` remains the orchestrator: reset stays first; present-pair/exposure/runtime checks precede image and environment work; one captured exposure descriptor set drives both security checks and later mounts/snapshots; protected values exist only in the final identity-pinned runtime subprocess. The guest entrypoint reserves one namespace for all scratch variables.

**Tech Stack:** Bash 4.0 language features, Microsandbox CLI 0.6.12–0.x for present-pair launches, Podman, pytest/Python test harness, existing Arch Linux guest image. Sensitive new traversal/path/identity logic SHALL use Bash builtins (`[[ -e/-L/-d/-f/-r/-x/-ef ]]`, parameter expansion, `cd -P`, `pwd -P`, `read`, `printf`) and a cycle-aware recursive glob walker—not ambient `find`, `realpath`, `stat`, `sort`, or `xargs`. The only external helpers allowed after reset are `tr`, `cmp`, `mktemp`, `chmod`, and `rm`; Task 3 resolves, project/exposure-vets, identity-pins, and invokes their absolute paths. Use no Bash feature introduced after 4.0 and no GNU-only helper option; this preserves the repository's Linux/macOS contract without adding a host dependency.

**Standards:** Apply the shared code standards in every task: DRY, low cyclomatic complexity, type safety, no unnecessary abstractions or fallbacks, no hacks or workarounds, informative docstrings, documentation of current state only.

**Feature spec:** `docs/design/2026-08-21-project-secret-injection-spec.md` (the behavioral contract)

---

## Commands

Run all commands from `/workspace/.worktrees/project-secret-injection`.

- Install dependencies: `uv sync`
- Focused library tests: `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py -q`
- Launcher/security tests: `env -u TAU_LAN_HOSTS uv run pytest tests/test_run.py tests/test_security.py -q`
- Config tests: `env -u TAU_LAN_HOSTS uv run pytest tests/test_config.py tests/test_containerfile.py -q`
- Integration tests: `env -u TAU_LAN_HOSTS uv run pytest tests/test_integration.py -q` (compatible `msb`, Podman, and Linux KVM or macOS virtualization required; otherwise explicit prerequisite skips)
- Full suite: `env -u TAU_LAN_HOSTS uv run pytest tests/`
- Shell syntax: `bash -n run.sh install.sh lib/project-secrets.sh config/entrypoint.sh`
- Python syntax: `uv run python -m compileall -q tests`
- Whitespace validation: `git diff --check`
- No formatter, linter, or static type checker is configured; do not claim those checks ran.

## File Structure and State Contract

- Create `lib/project-secrets.sh` — the single owner of project-secret state and behavior.
- Create `tests/test_project_secrets.py` — direct no-KVM library tests through fresh Bash processes.
- Modify `run.sh` — orchestration and one-time exposure-source capture.
- Modify `config/entrypoint.sh` — reserved scratch namespace.
- Modify `tests/test_config.py`, `tests/test_run.py`, `tests/test_security.py`, `tests/conftest.py`, and `tests/test_integration.py` — layered verification.
- Modify `README.md`, `config/APPEND_SYSTEM.md`, and `docs/SPEC.md` — current documentation and living requirements.

The library exposes these state names after successful discovery; later APIs consume this state rather than accept ambiguous roots:

- `PROJECT_SECRETS_PAIR_STATE` — exactly `none` or `present`.
- `PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL`, `PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL`.
- `PROJECT_SECRETS_DIR_LEXICAL`, `PROJECT_SECRETS_DIR_PHYSICAL`.
- `PROJECT_SECRETS_VALUES_LEXICAL`, `PROJECT_SECRETS_VALUES_PHYSICAL`.
- `PROJECT_SECRETS_POLICY_LEXICAL`, `PROJECT_SECRETS_POLICY_PHYSICAL`.

Absent sources are represented only by `PROJECT_SECRETS_PAIR_STATE=none`; no sentinel path is accepted. Discovery is single-use in production. Once populated, path state is readonly before `TAU_ENV_FILE` is sourced. The library registers all public and private functions readonly before ordinary trusted code runs; an attempted replacement is an ordinary-config error and occurs before any real project value is retained.

### Task 1: Reserve the Entrypoint Internal Namespace

**Files:**
- Modify: `config/entrypoint.sh` — rename every scratch global, loop target, temporary, and function-local variable under `TAU_ENTRYPOINT_`.
- Modify: `tests/test_config.py` — mechanically enforce the namespace and update old internal-name assertions.

**Spec requirement:** Literal secret value grammar — entrypoint namespace rule and “Entrypoint namespace is reserved.”

**Interface:**
- `link_volume_dir(backing_path, target_path) -> status` keeps the same call contract; all locals begin `TAU_ENTRYPOINT_`.
- Guest contract exports remain exactly `HOME`, `SHELL`, `TERM`, `COLORTERM`, `USER`, `LOGNAME`, `PATH`, `PYTHONUSERBASE`, `NPM_CONFIG_PREFIX`, `PIP_USER`, and `TAU_NO_UPDATE_CHECK`.
- Entrypoint argv handoff and exit status remain unchanged.

**Behavior:**
- Session/log migration, credentials, config refresh/removal, shell seeding, npm defaults, exports, and command execution do not change.
- No scratch assignment can overwrite an accepted guest secret because all scratch names use the reserved prefix.

**Tests must prove:**
- `test_entrypoint_internal_variables_use_reserved_prefix` — assignment/local/loop targets are either enumerated guest exports or `TAU_ENTRYPOINT_*`.
- `test_entrypoint_has_required_directives` — synchronization, migration, and argv-handoff directives remain present after renaming.
- Existing config syntax and layout tests remain green; they are regression checks and are not expected to fail in RED.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_config.py -q && bash -n config/entrypoint.sh && git diff --check` — expected: pass.

- [ ] Add the namespace test and confirm only that new test fails for the expected old scratch names
- [ ] Rename internal variables without changing behavior
- [ ] Run verification
- [ ] Commit: `git add config/entrypoint.sh tests/test_config.py && git commit -m "refactor: reserve entrypoint variable namespace" -m $'Moves all entrypoint scratch state under one prefix.\nPreserves startup, synchronization, and guest exports.'`

### Task 2: Implement Exact Discovery and Pair-State Validation

**Files:**
- Create: `lib/project-secrets.sh` — lexical/physical path helpers, exact mapping, pair shape/type validation, state contract, cleanup shell.
- Create: `tests/test_project_secrets.py` — fresh-process Bash harness and discovery/pair tests.

**Spec requirement:** Reset bypasses secret discovery (library non-use contract); Exact secret location mapping; Early source preflight source-shape scenarios.

**Interface:**
- `project_secrets_discover(launch_path, home_path, projects_mode, projects_value) -> status` — `projects_mode` is exactly `default` or `explicit`; sets the exact public state listed above and emits no stdout. The only candidate basenames are `${PROJECT_SECRETS_DIR_LEXICAL}/secrets.env` and `${PROJECT_SECRETS_DIR_LEXICAL}/secrets.yaml`; differently named files never satisfy the pair. Invalid input returns nonzero with class/path diagnostics but no source content.
- `project_secrets_cleanup() -> status` — initially idempotent with no staging path; later tasks extend it without changing callers.
- Private `project_secrets_lexical_path(path, base) -> normalized_absolute_path` — removes dot segments without following links using Bash parameter expansion.
- Private `project_secrets_physical_path(path) -> canonical_absolute_path` — resolves directory components through `cd -P`/`pwd -P` and verifies the final entry with Bash file predicates; it does not invoke ambient path tools.
- Private ancestry/identity predicates return distinct success/not-contained/error results; uncertainty propagates as failure.

**Behavior:**
- Physical project membership is authoritative. Lexical-out/physical-in maps; lexical-in/physical-out disables.
- `${HOME}/Projects/megali/main` maps to `${HOME}/.megali/main`; no parent inheritance.
- Missing default root, exact directory, or both source entries produces `none`. Explicit empty/missing and existing invalid default/explicit roots fail.
- Existing secret directory must be readable/searchable. Two readable regular files produce `present`; incomplete, dangling, unreadable, or non-regular entries fail without opening them.
- Content, runtime, images, environment files, and exposed trees are untouched in this task.
- New code uses Bash/common BSD-GNU command subsets only; tests exercise paths containing spaces and symlinks.

**Tests must prove:**
- `test_project_and_nested_mapping_are_exact` — `.megali`, `.megali/main`, no inheritance.
- `test_projects_root_and_outside_launch_select_none` — equality/outside scenarios.
- `test_relative_explicit_root_resolves_from_launch_directory` — relative override semantics.
- `test_physical_membership_wins_symlink_disagreement` — both disagreement directions.
- `test_default_root_states` — absent default disables; dangling/file/unreadable/unsearchable existing default fails.
- `test_explicit_root_states` — empty/missing/dangling/file/unreadable/unsearchable explicit fails.
- `test_secret_directory_states` — absent disables; dangling/file/unreadable/unsearchable fails.
- `test_pair_states_and_exact_basenames` — only `secrets.env` plus `secrets.yaml` mark present; differently named files do not substitute; both absent disables; one-sided, dangling, directory, FIFO, socket, device, unreadable reject without blocking.
- Test docstrings state what invariant is proved and why.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py -q && bash -n lib/project-secrets.sh && git diff --check` — expected: pass.

- [ ] Add `test_library_exports_discovery_api` first; confirm it fails because the discovery interface is absent
- [ ] Create a sourceable library with the discovery interface and implement the first exact-mapping RED/GREEN cycle
- [ ] Add and implement each remaining discovery/pair behavior incrementally, keeping prior tests green
- [ ] Run verification
- [ ] Commit: `git add lib/project-secrets.sh tests/test_project_secrets.py && git commit -m "feat: discover exact project secret sources" -m $'Adds physical project mapping and exact paired-source states.\nCovers invalid roots, directories, and file types.'`

### Task 3: Add Shared Exposure Descriptors and Runtime Trust

**Files:**
- Modify: `lib/project-secrets.sh` — descriptor registry, recursive exposure/identity scanner, runtime resolution/identity/version gate.
- Modify: `tests/test_project_secrets.py` — exposure, alias, traversal failure, and runtime tests.

**Spec requirement:** Early source preflight ordering; Host-only source isolation; Compatible secret runtime.

**Interface:**
- `project_secrets_register_exposed_source(kind, lexical_path) -> status` — `kind` is exactly `tree-no-follow`, `tree-dereference`, or `file`; records lexical and physical identity using builtins. Missing optional paths are not registered. Only an exact duplicate `(kind, normalized lexical path)` is deduplicated; descriptors with another lexical alias or traversal kind are retained even when physical identity matches, so no-follow registration can never suppress required dereference traversal.
- `project_secrets_pin_helpers(initial_path) -> status` — called only after every exposure descriptor and projects-root scan are registered; using Bash builtins only, resolves external `tr`, `cmp`, `mktemp`, `chmod`, and `rm`; rejects helpers lexically/physically inside or hard-linked anywhere under the projects root or any registered workspace/build/config/agents/credentials/prompt descriptor; records readonly absolute paths and identities. Every later helper invocation uses and revalidates these paths.
- `project_secrets_register_projects_root_scan() -> status` — registers the entire physical projects root as a no-follow hard-link scan domain when pair state is `present`.
- `project_secrets_preflight_exposure() -> status` — consumes only registered descriptors and present-pair state; checks component overlap, file identity, recursively reachable config-link targets, and hard links before image/runtime/content work.
- `project_secrets_resolve_runtime(initial_path) -> status` — resolves only an external executable from the initial host PATH, rejects overlap/hard links against projects root and every descriptor, stores readonly `PROJECT_SECRETS_MSB_PATH` and identity.
- `project_secrets_check_runtime() -> status` — exact successful stdout/stderr/newline/semantic-range contract.
- `project_secrets_revalidate_runtime() -> status` — exact path identity check before value retention.

**Descriptor contract:**
- `tree-no-follow`: recursively inspect directory entries and hard-link identities without following nested symlinks; used for projects root, workspace, repository build context, and `.agents`.
- `tree-dereference`: inspect the same file graph later copied with dereferencing, following links, detecting cycles/dangling/unreadable targets, and failing on uncertainty; used only for already-filtered Tau bootstrap entries.
- `file`: direct mount/input identity and containment; used for shared credentials and immutable prompt.
- `run.sh` will capture the exact filtered Tau top-level entry list once and both register and snapshot that same list; the library does not independently rediscover exclusions.

**Behavior:**
- Source/exposure errors precede runtime and content errors.
- Builtin recursive walkers never follow links for no-follow trees, follow and cycle-check directory identities for dereference trees, and catch projects-root hard-link aliases in unlaunched siblings.
- Project-controlled or descriptor-aliased executable shadows for every allowed helper and `msb` reject before the helper can inspect source bytes, influence a security decision, or run after guest-writable mounts return.
- Every registered tree detects source hard links; dereferenced trees reject dangling/cyclic/unreadable paths and overlap in either direction.
- Case-insensitive aliases resolve through physical/file identity rather than string case.
- Runtime executable is external, canonical, outside/hard-link-free from all project/exposed/build trees, exact-output compatible `>=0.6.12,<1.0.0`, and identity pinned.
- Every post-resolution Microsandbox operation in a present-pair launch (`images`, `load`, `rmi`, `run`) uses `PROJECT_SECRETS_MSB_PATH`, never ambient `msb`.

**Tests must prove:**
- `test_sensitive_logic_uses_only_builtins_and_pinned_helpers` — static call-site check forbids ambient/path-specific tools and GNU-only options; Bash version gate accepts 4.0+ and code avoids post-4.0 syntax.
- `test_project_controlled_helper_shadows_and_hard_links_reject` — malicious `tr`, `cmp`, `mktemp`, `chmod`, and `rm` under projects/workspace/build never execute or inspect dummy source bytes.
- `test_each_helper_rejects_every_descriptor_alias` — each helper hard-linked or resolved into config, agents, credentials, or prompt descriptors rejects; guest-writable `rm` alias cannot run during cleanup.
- `test_exposure_error_precedes_runtime_and_content` — no version/content operation after overlap.
- `test_projects_root_hard_link_alias_rejects` — alias in an unlaunched sibling fails.
- `test_sources_inside_projects_root_reject_lexically_and_physically` — both component-containment scenarios, independent of hard links.
- `test_source_directory_overlap_rejects_both_directions_for_all_descriptors` — source ancestor and descendant cases for workspace, build, agents, filtered config, credentials, and prompt.
- `test_each_descriptor_kind_rejects_source_aliases` — workspace/build/agents/config/credentials/prompt.
- `test_dereference_tree_rejects_nested_overlap_and_bad_graphs` — nested target, dangling link, cycle, unreadable entry.
- `test_descriptor_alias_keeps_strict_dereference_semantics` — a Tau bootstrap descriptor physically aliasing an earlier no-follow tree still follows its nested link and rejects secret overlap.
- `test_case_alias_and_identity_uncertainty_fail_closed` — case alias where filesystem supports it; injected inspection failure everywhere.
- `test_runtime_version_process_contract` — boundaries plus old/future/leading-zero/pre/build/CRLF/blank/stderr/nonzero/malformed.
- `test_project_controlled_runtime_and_all_descriptor_aliases_reject` — runtime inside/hard-linked under every project/exposed/build domain.
- `test_runtime_identity_is_absolute_and_pinned` — PATH change cannot redirect; replacement fails.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py -q && bash -n lib/project-secrets.sh && git diff --check` — expected: pass.

- [ ] Add behavior-specific failing tests while keeping Task 2 tests green
- [ ] Implement descriptors, exposure traversal, and runtime trust
- [ ] Run verification
- [ ] Commit: `git add lib/project-secrets.sh tests/test_project_secrets.py && git commit -m "feat: preflight secret exposure and runtime" -m $'Adds shared exposure traversal and helper/runtime trust.\nCovers aliases, bad graphs, and compatible versions.'`

### Task 4: Implement Strict Value and Policy Grammars

**Files:**
- Modify: `lib/project-secrets.sh` — byte validator, metadata pass, reserved names, policy state machine, exact name-set/effective-policy state.
- Modify: `tests/test_project_secrets.py` — exhaustive grammar tests.

**Spec requirement:** Literal secret value grammar; Restricted secret policy grammar; Environment source isolation metadata half.

**Interface:**
- `project_secrets_validate_metadata() -> status` — valid only after compatible present-pair preflight; validates all bytes/lines/names/policy and exact sets without retaining values; sets readonly indexed `PROJECT_SECRET_NAMES` in value-file order and private effective-policy arrays.
- `project_secrets_is_guest_name(name) -> status` — zero only for a validated project guest name; emits nothing.
- `project_secrets_validate_env_source(env_file) -> status` — before sourcing, rejects lexical/physical/hard-link identity with present-pair value source.
- Private byte validator accepts printable ASCII plus CR/LF only and never stores unsupported bytes in Bash variables.
- All library functions and metadata/path/runtime state become readonly before returning from successful metadata validation; attempted function/state replacement by trusted ordinary config fails before values are retained.

**Behavior:**
- Value metadata pass validates but discards text after `=`; final values are not retained in this task.
- Complete value grammar: comments/blanks, CRLF, no-final-newline, literal printable data, whitespace-only values; NUL/control/non-ASCII/tab/malformed/duplicate/empty reject with class+line only.
- Reserved exact names and `BASH`, `TAU_ENTRYPOINT_`, `TAU_SANDBOX_SECRET_SOURCE_` prefixes reject.
- Complete policy grammar and DNS/injection/default semantics match the feature spec exactly; omitted inject creates effective headers-only policy.
- Destination lowercase canonicalization drives duplicates and generated state; exact/name-set matching is case-sensitive and non-empty.
- No arbitrary YAML parser or unsafe `eval` is introduced.

**Tests must prove:**
- `test_value_literal_and_line_boundaries` — all accepted forms and no shell execution.
- `test_value_byte_and_entry_failures_are_redacted` — every rejected byte/entry class, line only, no name/value.
- `test_reserved_name_categories` — entrypoint/source/Bash prefixes and every exact category.
- `test_policy_minimal_default_and_full_forms` — headers default, comments/blanks, quotes, wildcard, all locations, final unterminated item.
- `test_policy_syntax_rejection_matrix` — every unsupported syntax/field/line boundary.
- `test_destination_label_and_length_boundaries` — exact 253 and wildcard suffix/full 253/255 pass; all invalid forms fail.
- `test_policy_duplicates_empty_lists_and_name_mismatch` — canonical duplicates, fields/names/locations, empty data, case mismatch, no ambient fallback.
- `test_env_source_aliases_reject_without_execution` — lexical/physical/hard-link aliases containing command syntax never source.
- `test_metadata_and_functions_are_readonly_before_ordinary_config` — attempted replacements fail before value retention.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py -q && bash -n lib/project-secrets.sh && git diff --check` — expected: pass.

- [ ] Add grammar tests that fail for the expected acceptance/rejection mismatch while Tasks 2–3 remain green
- [ ] Implement metadata and policy parsing
- [ ] Run verification
- [ ] Commit: `git add lib/project-secrets.sh tests/test_project_secrets.py && git commit -m "feat: parse strict project secret policy" -m $'Adds literal value metadata and restricted policy parsing.\nCovers reserved names, destinations, and exact name sets.'`

### Task 5: Generate Private Policy and Isolate Final Invocation

**Files:**
- Modify: `lib/project-secrets.sh` — parent-owned staging lifecycle, source allocation, final value pass, scoped-config generation, clean subprocess invocation, cleanup.
- Modify: `tests/test_project_secrets.py` — lifecycle, allocation, generated schema, tracing/function isolation, subprocess-only values.

**Spec requirement:** Collision-free source references; Protected secret boundary; Environment source isolation instrumentation half; MODIFIED Environment forwarding causal boundary.

**Interface:**
- `project_secrets_create_staging() -> status` — parent process creates one mode-`0700` directory under fixed `/tmp`, records readonly `PROJECT_SECRETS_STAGING_DIR` and `PROJECT_SECRETS_GENERATED_CONF`, registers/checks them against every exposure descriptor, and predefines a mode-`0600` policy path. It runs before `TAU_ENV_FILE` but after metadata validation and contains no values.
- `project_secrets_note_ordinary_guest_name(name) -> status` — records one validated ordinary forwarded name in a private collision set.
- Private `project_secrets_prepare_runtime() -> status` — callable only inside the clean child; revalidates runtime/staging identity and modes, reparses/retains values, allocates synthetic names, writes exact generated policy.
- `project_secrets_exec_runtime(msb_argv: variadic string sequence) -> status` — requires first argument exactly `run`; scans only runtime options before the first exact `--` guest-command separator and rejects both `--secret-conf PATH` and `--secret-conf=PATH` there; preserves every guest argv element after `--`, including literal `--secret-conf` and `--secret-conf=PATH`; owns insertion of exactly `--secret-conf "$PROJECT_SECRETS_GENERATED_CONF"` immediately after `run`. Parent launches a synchronous subshell; child clears xtrace and DEBUG/RETURN/ERR/EXIT traps/tracing state, removes every non-library shell function, invokes required builtins with `builtin`, uses only readonly library/functions and identity-revalidated pinned helper paths, prepares values/policy, exports synthetic sources, then `exec`s the pinned runtime with the exact constructed argv. Parent cleanup uses its pinned absolute removal tool. Parent waits, removes staging, and returns the exact runtime status; parent `EXIT` cleanup is a second idempotent safety path.
- No child `EXIT` trap owns cleanup; `exec` intentionally replaces only the clean child, leaving the launcher parent to clean.

**Behavior:**
- Trusted `TAU_ENV_FILE` may intentionally read/output host data (out of boundary), but cannot replace readonly functions/state. Attempts fail before value retention.
- Inherited tracing/traps/functions cannot observe final value parsing/export.
- Synthetic candidates skip process env, ordinary names, project names, and prior allocations.
- Generated policy uses exactly one double-quoted guest-name mapping key per secret and exactly three fields in order: `value` as the double-quoted literal `${TAU_SANDBOX_SECRET_SOURCE_<n>}` reference, `allow` as a list of double-quoted lowercase destinations, and `inject` as a list of unquoted exact identifiers. No additional field is emitted; omission of `require_tls_identity` preserves the compatible runtime's secure `true` default; real values are absent.
- Compatible runtime receives sources literally; `${OTHER}` text is not expanded by launcher.
- Staging remains present for runtime config loading, then is removed on success, failure, signal/parent exit, and repeated cleanup.

**Tests must prove:**
- `test_parent_owned_staging_survives_exec_and_cleans_after_status` — fake runtime can open policy; parent then removes it and preserves status.
- `test_library_inserts_single_secret_conf_at_exact_position` — final argv is `run --secret-conf <readonly-generated-path> <original-runtime-rest> -- <original-guest-argv>`; separate and equals-form runtime duplicates plus non-run operations reject before values, while guest arguments using either spelling remain byte-for-byte argv elements.
- `test_staging_modes_identity_and_exposure_checks` — 0700/0600, fixed `/tmp`, overlap/alias rejects, replacement rejects.
- `test_multiple_source_candidate_collisions_are_skipped` — every collision set and later candidate.
- `test_generated_policy_exact_effective_model` — exact `value`/`allow`/`inject` field structure and order, quoting, no extra fields, quoted implicit-scalar names, header default, exact allowed/omitted destinations/locations, and no plaintext.
- `test_sources_exist_only_in_exec_replacement` — parent/original guest names unchanged; fake runtime sees synthetic vars; guest `-e` set has none.
- `test_literal_source_reference_text_is_not_launcher_expanded` — fake runtime process environment receives printable `${OTHER}` exactly, while the launcher never looks up `OTHER`.
- `test_xtrace_traps_and_replacement_attempts_cannot_disclose_values` — no dummy value in output; readonly replacement fails before values.
- `test_command_and_builtin_function_shadows_cannot_observe_or_block_values` — trusted config defines observer functions for every builtin/external command used by final parsing, generation, invocation, and cleanup; non-library functions are removed or bypassed, no observer sees values, exact runtime runs, and cleanup succeeds.
- `test_intentional_trusted_output_is_explicitly_outside_boundary` — deliberate pre-isolation dummy read is distinguishable from launcher output.
- `test_cleanup_success_failure_signal_and_idempotence` — all lifecycle exits remove only owned staging.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py -q && bash -n lib/project-secrets.sh && git diff --check` — expected: pass.

- [ ] Add lifecycle/isolation tests while earlier library tests remain green
- [ ] Implement staging, generation, and invocation
- [ ] Run verification
- [ ] Commit: `git add lib/project-secrets.sh tests/test_project_secrets.py && git commit -m "feat: prepare isolated secret runtime" -m $'Adds private policy staging and synthetic source allocation.\nIsolates value handling and preserves runtime status.'`

### Task 6: Integrate the Library into `run.sh`

**Files:**
- Modify: `run.sh` — reset ordering, raw path capture, shared descriptor enumeration, preflight ordering, exact runtime use, forwarding suppression, cleanup composition, final invocation.
- Modify: `tests/test_run.py` — fake runtime logging/version/identity support and launcher scenarios.
- Modify: `tests/test_security.py` — exposure/non-disclosure/network assertions.

**Spec requirement:** Reset bypasses discovery; Exact mapping; Early preflight; Host-only isolation; Compatible runtime; Collision-free references; Environment source isolation; MODIFIED Environment forwarding.

**Interface:**
- New setting `TAU_PROJECTS_DIR`: unset means default; explicitly empty invalid.
- `run.sh --reset` CLI/output stay unchanged and execute before project-secret discovery/library calls.
- `run.sh [COMMAND ...]` stays unchanged. For a present pair, `run.sh` passes the ordinary runtime argument sequence beginning with `run` to `project_secrets_exec_runtime`; the library alone inserts exactly one generated `--secret-conf PATH` immediately after `run`. No-pair launch has no secret/version/config behavior.
- `TAU_BOOTSTRAP_ENTRIES` is one captured indexed list of exact top-level config entries after existing exclusions. The same list registers `tree-dereference` exposure and later creates snapshots, preventing divergent enumeration.
- Raw lexical config/agents/credentials/prompt/build/workspace paths are captured before existing canonicalization; physical forms come from descriptor registration.
- All present-pair `msb images/load/rmi/run` calls use the saved absolute executable. Reset/no-pair behavior retains existing command lookup.
- One launcher cleanup calls library cleanup and bootstrap cleanup idempotently and preserves status.

**Behavior:**
- Present-pair source/exposure preflight occurs before `msb images`, Podman, ordinary env sourcing, config copying, and mount construction.
- Runtime compatibility occurs before metadata/value content errors. Metadata/name/policy validation occurs before ordinary forwarding. Staging and final values follow trusted ordinary config in protected lifecycle.
- `TAU_ENV_FILE` collision rejects before source; matching project names suppress `-e`; unrelated ordinary variables remain unchanged.
- A sourced PATH change cannot redirect any present-pair Microsandbox operation.
- Values are absent from argv, fake logs, output, Podman, mounts, generated policy, and guest env args.
- Secret allow hosts never generate `--net-rule`; existing `TAU_LAN_HOSTS` behavior remains the only LAN exception.

**Tests must prove:**
- `test_reset_bypasses_invalid_sources_and_incompatible_runtime` — volume removal and no secret/version calls.
- `test_root_outside_relative_and_nested_mapping_contract` — every exact mapping/no-secret scenario through launcher.
- `test_exposure_preflight_precedes_images_build_env_snapshot_mounts` — observable no-call ordering.
- `test_incompatible_runtime_precedes_empty_or_malformed_content` — exact error/call sequence.
- `test_no_pair_preserves_existing_invocation_and_old_runtime` — regression.
- `test_present_pair_uses_pinned_msb_for_images_load_rmi_and_run` — ambient replacement receives no operation.
- `test_shared_bootstrap_entry_list_drives_scan_and_snapshot` — exclusions and nested links cannot diverge.
- `test_generated_secret_conf_is_only_secret_argument` — library-owned option appears exactly once immediately after `run`; path present, sources/values absent; caller duplicate rejects.
- `test_same_name_suppresses_raw_forwarding_but_unrelated_forwards` — project collision is omitted; unrelated ordinary value appears only in the required `msb run -e` argument, never stdout, stderr, Podman arguments, or image inputs.
- `test_env_alias_and_trusted_instrumentation_contract` — alias never executes; xtrace/traps cannot see values; deliberate trusted output remains declared outside.
- `test_cleanup_after_runtime_success_and_failure` — staging/bootstrap both removed, status preserved.
- `test_no_launcher_channel_contains_dummy_value` — output/argv/config/build/mount/log assertions; simulated reflected response is classified as guest/service data, not launcher placement.
- `test_secret_hosts_do_not_add_network_rules` — runtime invocation contains no rule derived from secret hosts and existing network tests remain exact.
- Existing image/package/config/credential/resource/security tests remain green as regression checks.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_project_secrets.py tests/test_run.py tests/test_security.py -q && bash -n run.sh lib/project-secrets.sh && git diff --check` — expected: pass.

- [ ] Add launcher tests whose assertions fail on current ordering/arguments while existing regressions remain green
- [ ] Integrate the library and exact descriptor list
- [ ] Run verification
- [ ] Commit: `git add run.sh tests/test_run.py tests/test_security.py && git commit -m "feat: inject exact-directory project secrets" -m $'Integrates early preflight and protected runtime invocation.\nPreserves ordinary forwarding, images, mounts, and networking.'`

### Task 7: Verify the Real Compatible Runtime Boundary

**Files:**
- Modify: `tests/conftest.py` — explicit integration prerequisites and compatible-version marker.
- Modify: `tests/test_integration.py` — exact external fixture, placeholder/source absence, literal and reserved-name integration.

**Spec requirement:** Compatible runtime; Collision-free source references; Protected secret boundary; Host-only isolation.

**Interface:**
- `compatible_msb_secrets() -> bool` implements the exact successful version/range contract without reading environment secrets.
- Integration prerequisite checks cover `msb`, Podman, Linux `/dev/kvm` (or supported macOS), and compatible present-pair runtime before image/secret tests; skips state the exact missing prerequisite.
- `ProjectSecretFixture` exposes `projects_root`, `project_dir`, `secret_dir`, and derived `volumes`, plus `activate() -> context manager`. Entering creates the project and non-sensitive exact mapped pair; leaving removes the host secret directory and volumes and marks `cleanup_complete=True`. Tests keep the fixture object, exit the context, then assert cleanup from outside teardown.

**Behavior:**
- Real compatible runtime gives guest placeholder and withholds synthetic source variables/values.
- Printable special-character source text remains host-side.
- Runtime policy encodes effective allow/inject data; internal network substitution remains delegated to bounded Microsandbox contract, with no third-party endpoint.
- Network independence remains a deterministic fake-argv test in Task 6, not a nondeterministic private-service integration test.

**Tests must prove:**
- `test_project_secret_is_guest_placeholder_only` — `$MSB_TEST_PROJECT_API_KEY`, not dummy value.
- `test_synthetic_sources_are_absent_from_guest_environment`.
- `test_literal_source_text_reaches_runtime_source_environment_exactly` — fake-runtime coverage from Task 5 proves the exact literal; real guest still sees only placeholder.
- `test_reserved_entrypoint_bash_and_runner_names_reject_before_boot` — representative categories.
- `test_external_secret_fixture_and_volumes_are_cleaned` — after the fixture context exits, `cleanup_complete` is true and host directory/volumes are absent.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_integration.py -q && git diff --check` — expected: pass with prerequisites or explicit prerequisite skips.

- [ ] Implement and unit-check prerequisite detection plus the fixture/teardown infrastructure first; run existing integration tests and keep them green or explicitly skipped
- [ ] Add each compatible-runtime behavior test one at a time and confirm it fails for the missing feature behavior, not fixture setup
- [ ] Run verification
- [ ] Commit: `git add tests/conftest.py tests/test_integration.py && git commit -m "test: verify project secret placeholders" -m $'Adds compatible-runtime placeholder and source-isolation coverage.\nUses disposable external secret directories and volumes.'`

### Task 8: Publish Documentation and Living Requirements

**Files:**
- Modify: `README.md` — all user/configuration/security/testing/prerequisite behavior.
- Modify: `config/APPEND_SYSTEM.md` — ordinary values versus protected placeholders.
- Modify: `docs/SPEC.md` — synchronize approved requirements as current behavior.
- Modify: `tests/test_config.py` — documentation contract assertions.

**Spec requirement:** Project secret documentation; all Sandbox Launch requirements become living behavior.

**Interface:**
- README documents exact paired paths/formats with valid examples, `TAU_PROJECTS_DIR`, no inheritance, reserved names, present-pair gate `>=0.6.12,<1.0.0`, header default/all supported locations, DNS destination grammar, TLS/runtime boundary, forwarding precedence/trust, reset, and network independence.
- APPEND_SYSTEM states ordinary forwarded variables carry values while protected variables carry placeholders; `env` cannot reveal protected real values.
- `docs/SPEC.md` adds all approved current requirements/scenarios and replaces Environment forwarding without changing unrelated requirements or documenting history.

**Behavior:**
- README no longer recommends raw `TAU_ENV_FILE` forwarding for API credentials intended for protection and updates architecture, config table, filesystem, security model, tests, and prerequisites consistently.
- Guest reference does not imply protected values are inspectable.
- Living spec matches feature-spec headings/scenarios and current implementation.

**Tests must prove:**
- `test_append_system_doc_distinguishes_placeholders_and_ordinary_values` — both current paths accurate.
- `test_readme_has_valid_paired_examples_and_exact_mapping` — files, nested path, no inheritance.
- `test_readme_covers_grammar_and_reserved_names` — value/policy constraints, headers default, supported locations, destination limits.
- `test_readme_covers_gate_tls_network_reset_and_forwarding_precedence` — every mandated topic.
- `test_living_spec_contains_every_project_secret_requirement_and_scenario` — headings imported and unrelated sentinel requirements retained.

**Verify:** `env -u TAU_LAN_HOSTS uv run pytest tests/test_config.py -q && env -u TAU_LAN_HOSTS uv run pytest tests/ && bash -n run.sh install.sh lib/project-secrets.sh config/entrypoint.sh && uv run python -m compileall -q tests && git diff --check` — expected: 0 failures; integration skips only for explicit prerequisites.

- [ ] Add documentation tests and confirm they fail on missing current guidance
- [ ] Update documentation and living spec
- [ ] Run verification
- [ ] Commit: `git add README.md config/APPEND_SYSTEM.md docs/SPEC.md tests/test_config.py && git commit -m "docs: document protected project secrets" -m $'Documents exact source grammars and compatibility behavior.\nSynchronizes guest guidance and living requirements.'`

## Final Verification

- [ ] Run `env -u TAU_LAN_HOSTS uv run pytest tests/`
- [ ] Run `bash -n run.sh install.sh lib/project-secrets.sh config/entrypoint.sh`
- [ ] Run `uv run python -m compileall -q tests`
- [ ] Run `git diff --check`
- [ ] Confirm `git status --short` is empty
- [ ] Confirm the committed plan precedes eight coherent task commits and run `git log -9 --format='%s%n%b%x00'` to verify every task commit has the required subject plus a 2-line body
