#!/bin/bash
# lib/project-secrets.sh — host-only project-secret discovery library.
#
# Maps a launch directory to the user's hidden per-project secret directory,
# validates the paired sources found there, keeps a shared registry of
# every host location the launch will expose (workspace, build context,
# host configuration, credentials, prompt), preflights the present pair
# against that registry with recursive alias/overlap traversal, and pins
# the trusted helper and runtime executables the launcher may use, and
# validates the strict value and policy grammars of a preflighted
# present pair whose runtime compatibility is established, without
# retaining real values, and freezes its functions and state readonly
# once validation succeeds. Every
# revalidation re-runs the executable-exposure rejection against the
# current registry, so a pinned executable hard-linked into an exposed
# domain after pinning never runs. The library is sourced by the
# launcher; it never writes to stdout, discovery never reads source file
# contents, metadata validation reads them only far enough to validate
# bytes, names, and policy before discarding value text, and failures
# are reported as a class and path (with a line number where one is
# available) only.
#
# Bash 4.0 compatible: Bash builtins and parameter expansion only.

# Public state contract (exact names). An empty string means "not
# established"; an absent pair is represented solely by
# PROJECT_SECRETS_PAIR_STATE=none — never by a sentinel path.
PROJECT_SECRETS_PAIR_STATE=""
PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL=""
PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL=""
PROJECT_SECRETS_DIR_LEXICAL=""
PROJECT_SECRETS_DIR_PHYSICAL=""
PROJECT_SECRETS_VALUES_LEXICAL=""
PROJECT_SECRETS_VALUES_PHYSICAL=""
PROJECT_SECRETS_POLICY_LEXICAL=""
PROJECT_SECRETS_POLICY_PHYSICAL=""

# Pinned trust state. PROJECT_SECRETS_MSB_PATH is the canonical absolute
# path of the accepted runtime executable; the helper paths are the pinned
# absolute locations of the only external tools the launcher may use. All
# are empty until pinned and readonly once set.
PROJECT_SECRETS_MSB_PATH=""
PROJECT_SECRETS_HELPER_TR=""
PROJECT_SECRETS_HELPER_CMP=""
PROJECT_SECRETS_HELPER_MKTEMP=""
PROJECT_SECRETS_HELPER_CHMOD=""
PROJECT_SECRETS_HELPER_RM=""

# Private staging state. Empty until project_secrets_create_staging
# succeeds; readonly from then on. The staging directory holds only the
# mode-0600 generated policy path, never value text.
PROJECT_SECRETS_STAGING_DIR=""
PROJECT_SECRETS_GENERATED_CONF=""
_PROJECT_SECRETS_STAGING_PHYS=""

# Private exposure-descriptor registry: one (kind, lexical, physical)
# triple per registration, in registration order.
_PROJECT_SECRETS_DESCRIPTOR_KINDS=()
_PROJECT_SECRETS_DESCRIPTOR_LEX=()
_PROJECT_SECRETS_DESCRIPTOR_PHYS=()
_PROJECT_SECRETS_DESCRIPTOR_COUNT=0

# Private sequencing state: "ok" only after a successful exposure
# preflight over the current registry, and runtime state "ok" only
# after a successful runtime version check; rediscovery and any later
# registration clear both, forcing a new preflight and a new
# compatibility check before runtime or metadata work.
_PROJECT_SECRETS_EXPOSURE_STATE=""
_PROJECT_SECRETS_RUNTIME_STATE=""

# Private walker state, reinitialized for every scan.
_PROJECT_SECRETS_WALK_STACK=()
_PROJECT_SECRETS_WALK_DONE=()
_PROJECT_SECRETS_WALK_ENTRIES=()
_PROJECT_SECRETS_ALIAS_PATHS=()
_PROJECT_SECRETS_ALIAS_CLASS=""
_PROJECT_SECRETS_SOURCE_DIR_PHYS=""
_PROJECT_SECRETS_SOURCE_IDS=()

# Private helper pinning tables: tool names, public state fields, and the
# file descriptors holding each pinned executable's inode identity.
# Reserved descriptor range 60-66: 60-64 pin the five helpers, 65 pins
# the runtime executable, and 66 reads the version stdout in
# check_runtime. No other code may open a descriptor in that range or a
# pin would be silently closed or shadowed.
_PROJECT_SECRETS_HELPER_NAMES=( tr cmp mktemp chmod rm )
_PROJECT_SECRETS_HELPER_FIELDS=(
    PROJECT_SECRETS_HELPER_TR
    PROJECT_SECRETS_HELPER_CMP
    PROJECT_SECRETS_HELPER_MKTEMP
    PROJECT_SECRETS_HELPER_CHMOD
    PROJECT_SECRETS_HELPER_RM
)
_PROJECT_SECRETS_HELPER_FDS=( 60 61 62 63 64 )
_PROJECT_SECRETS_HELPERS_PINNED=0
_PROJECT_SECRETS_MSB_FD=""

# _project_secrets_fail(class, path) — report a class/path diagnostic on
# stderr; path may be empty for value-level errors. Never emits source
# content. Returns success so callers control the failure return.
_project_secrets_fail() {
    printf 'project-secrets: %s: %s\n' "$1" "$2" >&2
    return 0
}

# _project_secrets_require_unfrozen() — succeed only while no
# successful metadata validation has frozen the library; afterwards
# every state-mutating lifecycle call fails with the
# metadata-already-validated class and the already-validated policy
# path, so a second call reports a class/path diagnostic instead of a
# raw shell readonly error.
_project_secrets_require_unfrozen() {
    if [[ -z "${PROJECT_SECRET_NAMES-}" ]]; then
        return 0
    fi
    _project_secrets_fail metadata-already-validated "$PROJECT_SECRETS_POLICY_LEXICAL"
    return 1
}

# _project_secrets_reset_state() — clear every public state field and the
# exposure-preflight and runtime-check markers, so a rediscovery can
# never inherit an "ok" preflight or an established runtime
# compatibility belonging to an earlier pair. Pinned helper and runtime
# identities are readonly by design and are revalidated on every use
# instead of being reset. Once a metadata validation has frozen the
# library the call fails with metadata-already-validated rather than
# attempting assignments the readonly state must reject.
_project_secrets_reset_state() {
    _project_secrets_require_unfrozen || return 1
    PROJECT_SECRETS_PAIR_STATE=""
    PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL=""
    PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL=""
    PROJECT_SECRETS_DIR_LEXICAL=""
    PROJECT_SECRETS_DIR_PHYSICAL=""
    PROJECT_SECRETS_VALUES_LEXICAL=""
    PROJECT_SECRETS_VALUES_PHYSICAL=""
    PROJECT_SECRETS_POLICY_LEXICAL=""
    PROJECT_SECRETS_POLICY_PHYSICAL=""
    _PROJECT_SECRETS_EXPOSURE_STATE=""
    _PROJECT_SECRETS_RUNTIME_STATE=""
}

# project_secrets_lexical_path(path, base) — print the absolute lexical
# form of path, resolving a relative path against base and removing "." and
# ".." segments with parameter expansion. Symlinks are never followed and
# the filesystem is never consulted; base must be absolute.
project_secrets_lexical_path() {
    local input="$1" base="$2" combined remaining component normalized
    if [[ "$input" == /* ]]; then
        combined="$input"
    elif [[ "$base" == "/" ]]; then
        combined="/$input"
    else
        combined="${base%/}/$input"
    fi
    normalized=""
    remaining="/${combined#/}"
    while [[ "$remaining" == /* ]]; do
        remaining="${remaining#/}"
        component="${remaining%%/*}"
        if [[ "$remaining" == */* ]]; then
            remaining="/${remaining#*/}"
        else
            remaining=""
        fi
        case "$component" in
            "" | ".") ;;
            "..")
                if [[ "$normalized" == "/"* && "$normalized" != "/" ]]; then
                    normalized="${normalized%/*}"
                    [[ -n "$normalized" ]] || normalized="/"
                fi
                ;;
            *) normalized="$normalized/$component" ;;
        esac
    done
    [[ -n "$normalized" ]] || normalized="/"
    printf '%s' "$normalized"
}

# project_secrets_physical_path(path) — print the canonical absolute form
# of an existing entry. Directory components are resolved through
# cd -P/pwd -P (following symlinks); the final entry is only verified with
# Bash file predicates, so a non-directory entry keeps its resolved parent
# directory and exact basename. Returns nonzero when the path cannot be
# resolved, so uncertainty propagates as failure.
project_secrets_physical_path() {
    local input="$1" base parent phys_parent
    if [[ -d "$input" ]]; then
        ( cd -P -- "$input" && pwd -P ) || return 1
        return 0
    fi
    [[ -e "$input" || -L "$input" ]] || return 1
    base="${input##*/}"
    [[ "$base" != "$input" ]] || return 1
    parent="${input%"$base"}"
    [[ -n "$parent" ]] || parent="/"
    phys_parent="$( cd -P -- "$parent" && pwd -P )" || return 1
    [[ -e "$phys_parent/$base" || -L "$phys_parent/$base" ]] || return 1
    printf '%s/%s' "$phys_parent" "$base"
}

# project_secrets_path_is_descendant(ancestor, path) — 0 when path lies
# strictly below ancestor by complete-component comparison (both must be
# normalized absolute paths), 1 when it does not, 2 when the comparison
# cannot be established.
project_secrets_path_is_descendant() {
    local ancestor="$1" path="$2"
    [[ "$ancestor" == /* && "$path" == /* ]] || return 2
    if [[ "$ancestor" == "/" ]]; then
        [[ "$path" == /* && "$path" != "/" ]] && return 0
        return 1
    fi
    [[ "$path" == "$ancestor"/* ]] && return 0
    return 1
}

# project_secrets_paths_identical(path_a, path_b) — 0 when both existing
# entries share filesystem identity, 1 when they do not, 2 when identity
# cannot be established.
project_secrets_paths_identical() {
    local a="$1" b="$2"
    [[ -e "$a" && -e "$b" ]] || return 2
    [[ "$a" -ef "$b" ]] && return 0
    return 1
}

# _project_secrets_require_dir(class, path) — succeed only when path has a
# directory entry that resolves to a readable and searchable directory;
# absent, dangling, non-directory, unreadable, and unsearchable entries all
# fail with the caller's class.
_project_secrets_require_dir() {
    local class="$1" path="$2"
    if [[ -d "$path" && -r "$path" && -x "$path" ]]; then
        return 0
    fi
    _project_secrets_fail "$class" "$path"
    return 1
}

# _project_secrets_source_state(path) — classify one exact source entry:
# 0 present and valid (readable regular file), 1 absent, 2 present but
# invalid (dangling, non-regular, or unreadable). Classifies by type
# predicates only and never opens the entry.
_project_secrets_source_state() {
    local path="$1"
    [[ -e "$path" || -L "$path" ]] || return 1
    [[ -f "$path" && -r "$path" ]] && return 0
    return 2
}

# project_secrets_discover(launch_path, home_path, projects_mode,
# projects_value) — establish the exact project-secret state for one
# launch. projects_mode is exactly "default" (${home}/Projects;
# projects_value ignored) or "explicit" (projects_value is the
# TAU_PROJECTS_DIR setting, resolved from the launch directory when
# relative). Physical project membership is authoritative. A
# rediscovery also clears any earlier exposure-preflight and runtime-check
# markers, so runtime work always requires a preflight and metadata
# validation always requires a fresh runtime check over the rediscovered
# pair.
# On success returns 0 with PROJECT_SECRETS_PAIR_STATE set to none (no
# secrets; every path field empty) or present (every path field set). On
# invalid input returns nonzero with a class/path diagnostic on stderr;
# nothing is written to stdout and source contents are never read or
# echoed.
project_secrets_discover() {
    local launch_path="$1" home_path="$2" projects_mode="$3" projects_value="$4"
    local launch_lex launch_phys home_lex root_lex root_phys root_class
    local rel secret_dir_lex secret_dir_phys values_lex policy_lex
    local values_state policy_state membership missing

    _project_secrets_reset_state || return 1

    case "$projects_mode" in
        default | explicit) ;;
        *) _project_secrets_fail invalid-projects-mode "$projects_mode"; return 1 ;;
    esac
    if [[ "$home_path" != /* || ! -d "$home_path" ]]; then
        _project_secrets_fail invalid-home "$home_path"
        return 1
    fi
    if [[ "$launch_path" != /* || ! -d "$launch_path" ]]; then
        _project_secrets_fail invalid-launch "$launch_path"
        return 1
    fi
    home_lex="$(project_secrets_lexical_path "$home_path" "$PWD")"
    launch_lex="$(project_secrets_lexical_path "$launch_path" "$PWD")"
    launch_phys="$(project_secrets_physical_path "$launch_lex")" || {
        _project_secrets_fail invalid-launch "$launch_lex"
        return 1
    }

    if [[ "$projects_mode" == "default" ]]; then
        root_class=invalid-projects-root
        root_lex="$home_lex/Projects"
        # An absent default root only disables discovery.
        if [[ ! -e "$root_lex" && ! -L "$root_lex" ]]; then
            PROJECT_SECRETS_PAIR_STATE=none
            return 0
        fi
    else
        root_class=invalid-TAU_PROJECTS_DIR
        if [[ -z "$projects_value" ]]; then
            _project_secrets_fail "$root_class" "$projects_value"
            return 1
        fi
        root_lex="$(project_secrets_lexical_path "$projects_value" "$launch_lex")"
    fi
    _project_secrets_require_dir "$root_class" "$root_lex" || return 1
    root_phys="$(project_secrets_physical_path "$root_lex")" || {
        _project_secrets_fail "$root_class" "$root_lex"
        return 1
    }

    membership=0
    project_secrets_path_is_descendant "$root_phys" "$launch_phys" || membership=$?
    if (( membership == 2 )); then
        _project_secrets_fail invalid-launch "$launch_phys"
        return 1
    fi
    if (( membership == 1 )); then
        PROJECT_SECRETS_PAIR_STATE=none
        return 0
    fi

    if [[ "$root_phys" == "/" ]]; then
        rel="${launch_phys#/}"
    else
        rel="${launch_phys#"$root_phys"/}"
    fi
    secret_dir_lex="$home_lex/.${rel}"

    if [[ ! -e "$secret_dir_lex" && ! -L "$secret_dir_lex" ]]; then
        PROJECT_SECRETS_PAIR_STATE=none
        return 0
    fi
    _project_secrets_require_dir invalid-secret-directory "$secret_dir_lex" || return 1
    secret_dir_phys="$(project_secrets_physical_path "$secret_dir_lex")" || {
        _project_secrets_fail invalid-secret-directory "$secret_dir_lex"
        return 1
    }

    values_lex="$secret_dir_lex/secrets.env"
    policy_lex="$secret_dir_lex/secrets.yaml"
    values_state=0
    _project_secrets_source_state "$values_lex" || values_state=$?
    policy_state=0
    _project_secrets_source_state "$policy_lex" || policy_state=$?
    if (( values_state == 2 )); then
        _project_secrets_fail invalid-secret-source "$values_lex"
        return 1
    fi
    if (( policy_state == 2 )); then
        _project_secrets_fail invalid-secret-source "$policy_lex"
        return 1
    fi
    if (( values_state != policy_state )); then
        if (( values_state == 1 )); then
            missing="$values_lex"
        else
            missing="$policy_lex"
        fi
        _project_secrets_fail missing-secret-source "$missing"
        return 1
    fi
    if (( values_state == 1 )); then
        PROJECT_SECRETS_PAIR_STATE=none
        return 0
    fi

    PROJECT_SECRETS_PAIR_STATE=present
    PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL="$root_lex"
    PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL="$root_phys"
    PROJECT_SECRETS_DIR_LEXICAL="$secret_dir_lex"
    PROJECT_SECRETS_DIR_PHYSICAL="$secret_dir_phys"
    PROJECT_SECRETS_VALUES_LEXICAL="$values_lex"
    PROJECT_SECRETS_VALUES_PHYSICAL="$secret_dir_phys/secrets.env"
    PROJECT_SECRETS_POLICY_LEXICAL="$policy_lex"
    PROJECT_SECRETS_POLICY_PHYSICAL="$secret_dir_phys/secrets.yaml"
    return 0
}

# ---------------------------------------------------------------------------
# Shared exposure descriptors.

# _project_secrets_paths_overlap(a, b) — 0 when the two normalized paths
# are equal or contain each other by complete components, 1 when they are
# disjoint, 2 when the comparison cannot be established.
_project_secrets_paths_overlap() {
    local a="$1" b="$2" relation
    if [[ "$a" == "$b" ]]; then
        return 0
    fi
    relation=0
    project_secrets_path_is_descendant "$a" "$b" || relation=$?
    if (( relation == 0 )); then
        return 0
    fi
    if (( relation == 2 )); then
        return 2
    fi
    relation=0
    project_secrets_path_is_descendant "$b" "$a" || relation=$?
    if (( relation == 0 )); then
        return 0
    fi
    return "$relation"
}

# _project_secrets_reject_source_overlap(lex, phys) — fail with an
# exposure diagnostic when any present-pair source path (lexical or
# physical) equals or contains, or is contained by, the given domain.
_project_secrets_reject_source_overlap() {
    local lex="$1" phys="$2" source_path relation
    for source_path in \
        "$PROJECT_SECRETS_DIR_LEXICAL" "$PROJECT_SECRETS_DIR_PHYSICAL" \
        "$PROJECT_SECRETS_VALUES_LEXICAL" "$PROJECT_SECRETS_VALUES_PHYSICAL" \
        "$PROJECT_SECRETS_POLICY_LEXICAL" "$PROJECT_SECRETS_POLICY_PHYSICAL"
    do
        relation=0
        _project_secrets_paths_overlap "$source_path" "$lex" || relation=$?
        if (( relation != 1 )); then
            _project_secrets_fail exposure-overlap "$source_path"
            return 1
        fi
        relation=0
        _project_secrets_paths_overlap "$source_path" "$phys" || relation=$?
        if (( relation != 1 )); then
            _project_secrets_fail exposure-overlap "$source_path"
            return 1
        fi
    done
    return 0
}

# _project_secrets_reject_source_alias(phys) — fail when the given domain
# entry shares filesystem identity with either source file, or when that
# identity cannot be established.
_project_secrets_reject_source_alias() {
    local phys="$1" source_path relation
    for source_path in "$PROJECT_SECRETS_VALUES_PHYSICAL" "$PROJECT_SECRETS_POLICY_PHYSICAL"; do
        relation=0
        project_secrets_paths_identical "$phys" "$source_path" || relation=$?
        if (( relation == 0 )); then
            _project_secrets_fail exposure-alias "$phys"
            return 1
        fi
        if (( relation == 2 )); then
            _project_secrets_fail exposure-identity-uncertain "$phys"
            return 1
        fi
    done
    return 0
}

# project_secrets_register_exposed_source(kind, lexical_path) — add one
# exposure descriptor to the shared registry. kind is exactly
# tree-no-follow, tree-dereference, or file. A path with no directory entry
# is an optional location and stays unregistered. Only an exact duplicate
# (kind, normalized lexical path) is deduplicated; a different kind or a
# lexical alias is always retained so a no-follow registration can never
# suppress a required dereference traversal of the same location. Any
# registration invalidates an earlier exposure preflight and runtime
# check. Identity is recorded with Bash builtins only.
project_secrets_register_exposed_source() {
    local kind="$1" lex phys index
    _project_secrets_require_unfrozen || return 1
    case "$kind" in
        tree-no-follow | tree-dereference | file) ;;
        *)
            _project_secrets_fail invalid-exposure-kind "$kind"
            return 1
            ;;
    esac
    lex="$(project_secrets_lexical_path "$2" "$PWD")"
    for (( index = 0; index < _PROJECT_SECRETS_DESCRIPTOR_COUNT; index++ )); do
        if [[ "${_PROJECT_SECRETS_DESCRIPTOR_KINDS[index]}" == "$kind" \
            && "${_PROJECT_SECRETS_DESCRIPTOR_LEX[index]}" == "$lex" ]]; then
            return 0
        fi
    done
    if [[ ! -e "$lex" && ! -L "$lex" ]]; then
        return 0
    fi
    phys="$(project_secrets_physical_path "$lex")" || {
        _project_secrets_fail invalid-exposure-source "$lex"
        return 1
    }
    case "$kind" in
        tree-no-follow)
            [[ -d "$phys" ]] || {
                _project_secrets_fail invalid-exposure-source "$lex"
                return 1
            }
            ;;
        tree-dereference)
            [[ -d "$phys" || -f "$phys" ]] || {
                _project_secrets_fail invalid-exposure-source "$lex"
                return 1
            }
            ;;
        file)
            [[ -f "$phys" ]] || {
                _project_secrets_fail invalid-exposure-source "$lex"
                return 1
            }
            ;;
    esac
    _PROJECT_SECRETS_DESCRIPTOR_KINDS+=( "$kind" )
    _PROJECT_SECRETS_DESCRIPTOR_LEX+=( "$lex" )
    _PROJECT_SECRETS_DESCRIPTOR_PHYS+=( "$phys" )
    _PROJECT_SECRETS_DESCRIPTOR_COUNT=$(( _PROJECT_SECRETS_DESCRIPTOR_COUNT + 1 ))
    _PROJECT_SECRETS_EXPOSURE_STATE=""
    _PROJECT_SECRETS_RUNTIME_STATE=""
    return 0
}

# project_secrets_register_projects_root_scan() — register the projects
# root as a no-follow scan domain for a present pair; a no-pair launch has
# nothing to scan and the call is a successful no-op.
project_secrets_register_projects_root_scan() {
    if [[ "$PROJECT_SECRETS_PAIR_STATE" == "present" ]]; then
        project_secrets_register_exposed_source tree-no-follow "$PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL"
        return
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Recursive exposure traversal (Bash builtins only).

# _project_secrets_walk_list_dir(dir) — set _PROJECT_SECRETS_WALK_ENTRIES
# to every entry of dir, hidden names included, without following links.
_project_secrets_walk_list_dir() {
    local dir="$1" restore_nullglob restore_dotglob
    restore_nullglob="$(shopt -p nullglob)"
    restore_dotglob="$(shopt -p dotglob)"
    shopt -s nullglob dotglob
    _PROJECT_SECRETS_WALK_ENTRIES=( "$dir"/* )
    eval "$restore_nullglob"
    eval "$restore_dotglob"
}

# _project_secrets_walk_check_alias(entry) — fail when an existing entry
# shares filesystem identity with any path in the active alias set, or
# when an alias path can no longer be inspected.
_project_secrets_walk_check_alias() {
    local entry="$1" alias_path
    for alias_path in "${_PROJECT_SECRETS_ALIAS_PATHS[@]}"; do
        if [[ ! -e "$alias_path" ]]; then
            _project_secrets_fail exposure-identity-uncertain "$alias_path"
            return 1
        fi
        if [[ "$entry" -ef "$alias_path" ]]; then
            _project_secrets_fail "$_PROJECT_SECRETS_ALIAS_CLASS" "$entry"
            return 1
        fi
    done
    return 0
}

# _project_secrets_reject_resolved_inside_source(phys, ancestor_too) —
# fail when a resolved dereference node lies inside the secret source
# directory; with ancestor_too nonzero, also when the source directory lies
# inside the node.
_project_secrets_reject_resolved_inside_source() {
    local phys="$1" ancestor_too="$2" source_dir="$_PROJECT_SECRETS_SOURCE_DIR_PHYS"
    if [[ "$phys" == "$source_dir" || "$phys" == "$source_dir"/* ]]; then
        _project_secrets_fail exposure-overlap "$phys"
        return 1
    fi
    if (( ancestor_too )) && [[ "$source_dir" == "$phys"/* ]]; then
        _project_secrets_fail exposure-overlap "$phys"
        return 1
    fi
    return 0
}

# _project_secrets_collect_source_ids(dir) — append dir and every real
# entry below it to _PROJECT_SECRETS_SOURCE_IDS; symlink entries are
# skipped because their identity is their target's, not their own.
_project_secrets_collect_source_ids() {
    local dir="$1" entry
    if [[ ! -r "$dir" || ! -x "$dir" ]]; then
        _project_secrets_fail exposure-identity-uncertain "$dir"
        return 1
    fi
    _PROJECT_SECRETS_SOURCE_IDS+=( "$dir" )
    _project_secrets_walk_list_dir "$dir"
    for entry in "${_PROJECT_SECRETS_WALK_ENTRIES[@]}"; do
        if [[ -L "$entry" ]]; then
            continue
        fi
        if [[ -d "$entry" ]]; then
            _project_secrets_collect_source_ids "$entry" || return 1
        elif [[ -f "$entry" ]]; then
            _PROJECT_SECRETS_SOURCE_IDS+=( "$entry" )
        fi
    done
    return 0
}

# _project_secrets_reject_resolved_on_source(node) — fail when a
# dereference node, after following its links, shares filesystem identity
# with the secret source directory or any real entry below it. Identity
# comparison follows symlinks, so this catches file targets that a path
# comparison cannot resolve.
_project_secrets_reject_resolved_on_source() {
    local node="$1" source_id
    for source_id in "${_PROJECT_SECRETS_SOURCE_IDS[@]}"; do
        if [[ "$node" -ef "$source_id" ]]; then
            _project_secrets_fail exposure-overlap "$node"
            return 1
        fi
    done
    return 0
}

# _project_secrets_walk_nofollow_dir(dir) — recursively inspect dir
# without following symlinks, rejecting alias files and any directory
# that cannot be read or searched.
_project_secrets_walk_nofollow_dir() {
    local dir="$1" entry
    if [[ ! -r "$dir" || ! -x "$dir" ]]; then
        _project_secrets_fail exposure-unreadable-entry "$dir"
        return 1
    fi
    _project_secrets_walk_list_dir "$dir"
    for entry in "${_PROJECT_SECRETS_WALK_ENTRIES[@]}"; do
        if [[ -L "$entry" ]]; then
            continue
        fi
        if [[ -d "$entry" ]]; then
            _project_secrets_walk_nofollow_dir "$entry" || return 1
        elif [[ -f "$entry" ]]; then
            _project_secrets_walk_check_alias "$entry" || return 1
        fi
    done
    return 0
}

# _project_secrets_walk_nofollow_root(phys) — walk a registered no-follow
# tree root, failing closed when the registered directory no longer
# resolves as a directory.
_project_secrets_walk_nofollow_root() {
    local phys="$1"
    if [[ ! -d "$phys" ]]; then
        _project_secrets_fail exposure-identity-uncertain "$phys"
        return 1
    fi
    _project_secrets_walk_nofollow_dir "$phys"
}

# _project_secrets_walk_deref_node(node) — inspect one node of a
# dereference tree: follow links, reject dangling targets, cycles
# (a directory identity repeated on the current path), unreadable
# directories, nodes resolving into the secret directory, and alias files.
_project_secrets_walk_deref_node() {
    local node="$1" phys entry done_path stack_path
    if [[ -L "$node" && ! -e "$node" ]]; then
        _project_secrets_fail exposure-dangling-link "$node"
        return 1
    fi
    if [[ -d "$node" ]]; then
        if [[ ! -r "$node" || ! -x "$node" ]]; then
            _project_secrets_fail exposure-unreadable-entry "$node"
            return 1
        fi
        _project_secrets_reject_resolved_on_source "$node" || return 1
        phys="$(project_secrets_physical_path "$node")" || {
            _project_secrets_fail exposure-identity-uncertain "$node"
            return 1
        }
        if [[ -n "$_PROJECT_SECRETS_SOURCE_DIR_PHYS" ]]; then
            _project_secrets_reject_resolved_inside_source "$phys" 1 || return 1
        fi
        for stack_path in "${_PROJECT_SECRETS_WALK_STACK[@]}"; do
            if [[ "$stack_path" == "$phys" ]]; then
                _project_secrets_fail exposure-graph-cycle "$node"
                return 1
            fi
        done
        for done_path in "${_PROJECT_SECRETS_WALK_DONE[@]}"; do
            if [[ "$done_path" == "$phys" ]]; then
                return 0
            fi
        done
        _PROJECT_SECRETS_WALK_STACK+=( "$phys" )
        _project_secrets_walk_list_dir "$node"
        for entry in "${_PROJECT_SECRETS_WALK_ENTRIES[@]}"; do
            _project_secrets_walk_deref_node "$entry" || return 1
        done
        unset "_PROJECT_SECRETS_WALK_STACK[$(( ${#_PROJECT_SECRETS_WALK_STACK[@]} - 1 ))]"
        _PROJECT_SECRETS_WALK_DONE+=( "$phys" )
    elif [[ -f "$node" ]]; then
        phys="$(project_secrets_physical_path "$node")" || {
            _project_secrets_fail exposure-identity-uncertain "$node"
            return 1
        }
        _project_secrets_walk_check_alias "$node" || return 1
        _project_secrets_reject_resolved_on_source "$node" || return 1
        if [[ -n "$_PROJECT_SECRETS_SOURCE_DIR_PHYS" ]]; then
            _project_secrets_reject_resolved_inside_source "$phys" 0 || return 1
        fi
    fi
    return 0
}

# _project_secrets_walk_registered_descriptors() — walk every registered
# descriptor with its own traversal semantics.
_project_secrets_walk_registered_descriptors() {
    local index kind phys
    for (( index = 0; index < _PROJECT_SECRETS_DESCRIPTOR_COUNT; index++ )); do
        kind="${_PROJECT_SECRETS_DESCRIPTOR_KINDS[index]}"
        phys="${_PROJECT_SECRETS_DESCRIPTOR_PHYS[index]}"
        case "$kind" in
            tree-no-follow)
                _project_secrets_walk_nofollow_root "$phys" || return 1
                ;;
            tree-dereference)
                if [[ ! -e "$phys" && ! -L "$phys" ]]; then
                    _project_secrets_fail exposure-identity-uncertain "$phys"
                    return 1
                fi
                _PROJECT_SECRETS_WALK_STACK=()
                _project_secrets_walk_deref_node "$phys" || return 1
                ;;
            file)
                if [[ ! -e "$phys" && ! -L "$phys" ]]; then
                    _project_secrets_fail exposure-identity-uncertain "$phys"
                    return 1
                fi
                _project_secrets_walk_check_alias "$phys" || return 1
                ;;
        esac
    done
    return 0
}

# _project_secrets_walk_projects_root_if_needed() — walk the physical
# projects root with the active alias set unless a registered no-follow
# descriptor already covers exactly that tree.
_project_secrets_walk_projects_root_if_needed() {
    local index
    for (( index = 0; index < _PROJECT_SECRETS_DESCRIPTOR_COUNT; index++ )); do
        if [[ "${_PROJECT_SECRETS_DESCRIPTOR_KINDS[index]}" == "tree-no-follow" ]] \
            && project_secrets_paths_identical \
                "${_PROJECT_SECRETS_DESCRIPTOR_PHYS[index]}" \
                "$PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL"; then
            return 0
        fi
    done
    _project_secrets_walk_nofollow_root "$PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL"
}

# _project_secrets_walk_all_domains() — walk every registered descriptor
# plus, for a present pair, the whole projects root, using the active
# alias set and alias class.
_project_secrets_walk_all_domains() {
    _PROJECT_SECRETS_WALK_STACK=()
    _PROJECT_SECRETS_WALK_DONE=()
    _project_secrets_walk_registered_descriptors || return 1
    if [[ "$PROJECT_SECRETS_PAIR_STATE" == "present" ]]; then
        _project_secrets_walk_projects_root_if_needed || return 1
    fi
    return 0
}

# project_secrets_preflight_exposure() — verify the present pair is not
# exposed through any registered descriptor or through the projects root:
# lexical and physical containment in both directions, direct filesystem
# identity, and recursive traversal that follows links only for
# tree-dereference descriptors, with cycle, dangling, and readability
# detection. Any inspection uncertainty fails closed. A no-pair launch has
# nothing to check and succeeds without marking exposure ok, so runtime
# work stays impossible for no-pair launches.
project_secrets_preflight_exposure() {
    local index lex phys
    _project_secrets_require_unfrozen || return 1
    if [[ "$PROJECT_SECRETS_PAIR_STATE" != "present" ]]; then
        return 0
    fi
    _PROJECT_SECRETS_EXPOSURE_STATE=""
    for (( index = 0; index < _PROJECT_SECRETS_DESCRIPTOR_COUNT; index++ )); do
        lex="${_PROJECT_SECRETS_DESCRIPTOR_LEX[index]}"
        phys="${_PROJECT_SECRETS_DESCRIPTOR_PHYS[index]}"
        _project_secrets_reject_source_overlap "$lex" "$phys" || return 1
        _project_secrets_reject_source_alias "$phys" || return 1
    done
    _project_secrets_reject_source_overlap \
        "$PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL" \
        "$PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL" || return 1
    _PROJECT_SECRETS_ALIAS_PATHS=(
        "$PROJECT_SECRETS_VALUES_PHYSICAL"
        "$PROJECT_SECRETS_POLICY_PHYSICAL"
    )
    _PROJECT_SECRETS_ALIAS_CLASS="exposure-alias"
    _PROJECT_SECRETS_SOURCE_DIR_PHYS="$PROJECT_SECRETS_DIR_PHYSICAL"
    _PROJECT_SECRETS_SOURCE_IDS=()
    _project_secrets_collect_source_ids "$PROJECT_SECRETS_DIR_PHYSICAL" || {
        _PROJECT_SECRETS_SOURCE_DIR_PHYS=""
        return 1
    }
    _project_secrets_walk_all_domains || {
        _PROJECT_SECRETS_SOURCE_DIR_PHYS=""
        return 1
    }
    _PROJECT_SECRETS_SOURCE_DIR_PHYS=""
    _PROJECT_SECRETS_EXPOSURE_STATE="ok"
    return 0
}

# ---------------------------------------------------------------------------
# Helper and runtime trust.

# _project_secrets_find_external(name, search_path) — print the external
# executable for name located in the given PATH value, using only Bash
# builtins; the ambient PATH is never consulted.
_project_secrets_find_external() {
    local name="$1" found
    found="$( export PATH="$2"; builtin type -P "$name" )" || return 1
    if [[ -z "$found" ]]; then
        return 1
    fi
    printf '%s' "$found"
}

# _project_secrets_pin_identity(fd, path) — open path on the given file
# descriptor so the pinned inode identity stays observable through
# /proc/self/fd; the descriptor survives path replacement.
_project_secrets_pin_identity() {
    local fd="$1" path="$2"
    if [[ ! -f "$path" || ! -r "$path" ]]; then
        return 1
    fi
    case "$fd" in
        60) exec 60<"$path" ;;
        61) exec 61<"$path" ;;
        62) exec 62<"$path" ;;
        63) exec 63<"$path" ;;
        64) exec 64<"$path" ;;
        65) exec 65<"$path" ;;
        *) return 1 ;;
    esac
    [[ -e "/proc/self/fd/$fd" ]]
}

# _project_secrets_identity_matches(fd, path) — 0 when path still exists
# and still names the inode pinned on fd.
_project_secrets_identity_matches() {
    local fd="$1" path="$2"
    [[ -e "$path" && -e "/proc/self/fd/$fd" && "$path" -ef "/proc/self/fd/$fd" ]]
}

# _project_secrets_require_helper(field) — succeed only when the named
# helper field is pinned, still names the inode pinned for it, and is
# still rejected from the projects root and every registered descriptor
# domain, reporting helper-unpinned, helper-identity, or helper-exposure
# otherwise. Every helper use passes this gate immediately before the
# invocation, so a helper replaced after pinning — or hard-linked into an
# exposed domain after pinning, an inode project content could rewrite in
# place — can never run.
_project_secrets_require_helper() {
    local field="$1" index fd path lex
    path="${!field-}"
    [[ -n "$path" ]] || {
        _project_secrets_fail helper-unpinned ""
        return 1
    }
    for (( index = 0; index < 5; index++ )); do
        if [[ "${_PROJECT_SECRETS_HELPER_FIELDS[index]}" == "$field" ]]; then
            fd="${_PROJECT_SECRETS_HELPER_FDS[index]}"
            if ! _project_secrets_identity_matches "$fd" "$path"; then
                _project_secrets_fail helper-identity "$path"
                return 1
            fi
            lex="$(project_secrets_lexical_path "$path" "$PWD")"
            _project_secrets_revet_exposure helper-exposure "$lex" "$path" || return 1
            return 0
        fi
    done
    _project_secrets_fail helper-unpinned "$field"
    return 1
}

# _project_secrets_invoke_helper(field, args...) — the only way the
# library runs a pinned helper: revalidate the named helper's pinned
# identity and exposure immediately before the invocation, then execute
# the pinned absolute path with the given arguments. A helper that fails
# when run reports helper-failed, keeping helper-identity for the gate
# and the pinned path the single trust anchor for every helper execution.
_project_secrets_invoke_helper() {
    local field="$1" path
    _project_secrets_require_helper "$field" || return 1
    shift
    path="${!field}"
    if ! "$path" "$@"; then
        _project_secrets_fail helper-failed "$path"
        return 1
    fi
    return 0
}

# _project_secrets_reject_executable_exposure(class, lex, phys) — reject
# an executable that lies inside or under, is equal to, or shares identity
# with the projects root or any registered descriptor domain; hard links
# under those domains are found by the caller's tree walk.
_project_secrets_reject_executable_exposure() {
    local class="$1" lex="$2" phys="$3" index relation
    for (( index = 0; index < _PROJECT_SECRETS_DESCRIPTOR_COUNT; index++ )); do
        relation=0
        _project_secrets_paths_overlap "$lex" "${_PROJECT_SECRETS_DESCRIPTOR_LEX[index]}" || relation=$?
        if (( relation != 1 )); then
            _project_secrets_fail "$class" "$phys"
            return 1
        fi
        relation=0
        _project_secrets_paths_overlap "$phys" "${_PROJECT_SECRETS_DESCRIPTOR_PHYS[index]}" || relation=$?
        if (( relation != 1 )); then
            _project_secrets_fail "$class" "$phys"
            return 1
        fi
        relation=0
        project_secrets_paths_identical "$phys" "${_PROJECT_SECRETS_DESCRIPTOR_PHYS[index]}" || relation=$?
        if (( relation != 1 )); then
            if (( relation == 2 )); then
                _project_secrets_fail exposure-identity-uncertain "${_PROJECT_SECRETS_DESCRIPTOR_PHYS[index]}"
            else
                _project_secrets_fail "$class" "$phys"
            fi
            return 1
        fi
    done
    if [[ "$PROJECT_SECRETS_PAIR_STATE" == "present" ]]; then
        relation=0
        _project_secrets_paths_overlap "$lex" "$PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL" || relation=$?
        if (( relation != 1 )); then
            _project_secrets_fail "$class" "$phys"
            return 1
        fi
        relation=0
        _project_secrets_paths_overlap "$phys" "$PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL" || relation=$?
        if (( relation != 1 )); then
            _project_secrets_fail "$class" "$phys"
            return 1
        fi
    fi
    return 0
}

# _project_secrets_revet_exposure(class, lex, phys) — re-run the full
# executable-exposure rejection for one already-pinned executable against
# the current registry: location overlap with every registered descriptor
# and the projects root, plus a recursive alias walk that finds hard
# links under any registered domain. Every revalidation runs this so an
# executable hard-linked into an exposed domain after pinning — an inode
# project content could rewrite in place — fails before it can run.
_project_secrets_revet_exposure() {
    local class="$1" lex="$2" phys="$3"
    _project_secrets_reject_executable_exposure "$class" "$lex" "$phys" || return 1
    _PROJECT_SECRETS_ALIAS_PATHS=( "$phys" )
    _PROJECT_SECRETS_ALIAS_CLASS="$class"
    _PROJECT_SECRETS_SOURCE_DIR_PHYS=""
    _project_secrets_walk_all_domains || return 1
    return 0
}

# project_secrets_pin_helpers(initial_path) — resolve the five allowed
# external helpers through the initial PATH, reject any located inside or
# under, or hard-linked into, the projects root or any registered
# descriptor domain, and pin their absolute paths and inode identities
# readonly. Callable only before a successful metadata validation; after
# the freeze the call fails with metadata-already-validated, like every
# other state-mutating lifecycle call, instead of attempting assignments
# the readonly pin state must reject. Later calls never re-resolve: they
# revalidate each pinned identity and re-reject each pinned executable
# against the current registry through the per-use helper gate, so a PATH
# change cannot redirect a helper and a newly exposed hard link cannot
# run one.
project_secrets_pin_helpers() {
    local initial="$1" index name field fd found lex phys
    local -a canonical=()
    _project_secrets_require_unfrozen || return 1
    if [[ "$_PROJECT_SECRETS_HELPERS_PINNED" == "1" ]]; then
        for (( index = 0; index < 5; index++ )); do
            _project_secrets_require_helper "${_PROJECT_SECRETS_HELPER_FIELDS[index]}" || return 1
        done
        return 0
    fi
    for (( index = 0; index < 5; index++ )); do
        name="${_PROJECT_SECRETS_HELPER_NAMES[index]}"
        found="$(_project_secrets_find_external "$name" "$initial")" || {
            _project_secrets_fail helper-unresolved "$name"
            return 1
        }
        lex="$(project_secrets_lexical_path "$found" "$PWD")"
        phys="$(project_secrets_physical_path "$found")" || {
            _project_secrets_fail helper-unresolved "$found"
            return 1
        }
        if [[ ! -f "$phys" || ! -x "$phys" ]]; then
            _project_secrets_fail helper-unresolved "$phys"
            return 1
        fi
        _project_secrets_reject_executable_exposure helper-exposure "$lex" "$phys" || return 1
        canonical+=( "$phys" )
    done
    _PROJECT_SECRETS_ALIAS_PATHS=( "${canonical[@]}" )
    _PROJECT_SECRETS_ALIAS_CLASS="helper-exposure"
    _PROJECT_SECRETS_SOURCE_DIR_PHYS=""
    _project_secrets_walk_all_domains || return 1
    for (( index = 0; index < 5; index++ )); do
        field="${_PROJECT_SECRETS_HELPER_FIELDS[index]}"
        fd="${_PROJECT_SECRETS_HELPER_FDS[index]}"
        phys="${canonical[index]}"
        if ! _project_secrets_pin_identity "$fd" "$phys"; then
            _project_secrets_fail helper-identity "$phys"
            return 1
        fi
        readonly "$field=$phys"
    done
    _PROJECT_SECRETS_HELPERS_PINNED=1
    return 0
}

# project_secrets_resolve_runtime(initial_path) — resolve the runtime
# executable through the initial host PATH only after a successful
# exposure preflight, canonicalize it, reject it from the projects root
# and every registered descriptor domain (location, equality, and hard
# links), and pin its absolute path and identity readonly as
# PROJECT_SECRETS_MSB_PATH. Callable only before a successful metadata
# validation; after the freeze the call fails with
# metadata-already-validated instead of attempting assignments the
# readonly pin state must reject, while project_secrets_revalidate_runtime
# keeps vetting the pinned executable over the frozen registry. Later
# calls never re-resolve: they revalidate the pinned executable,
# re-running the identity check and the executable-exposure rejection
# against the current registry.
project_secrets_resolve_runtime() {
    local initial="$1" found lex phys
    _project_secrets_require_unfrozen || return 1
    if [[ "$_PROJECT_SECRETS_EXPOSURE_STATE" != "ok" ]]; then
        _project_secrets_fail exposure-not-preflighted ""
        return 1
    fi
    if [[ -n "${PROJECT_SECRETS_MSB_PATH-}" ]]; then
        project_secrets_revalidate_runtime && return 0
        return 1
    fi
    found="$(_project_secrets_find_external msb "$initial")" || {
        _project_secrets_fail runtime-unresolved "msb"
        return 1
    }
    lex="$(project_secrets_lexical_path "$found" "$PWD")"
    phys="$(project_secrets_physical_path "$found")" || {
        _project_secrets_fail runtime-unresolved "$found"
        return 1
    }
    if [[ ! -f "$phys" || ! -x "$phys" ]]; then
        _project_secrets_fail runtime-unresolved "$phys"
        return 1
    fi
    _project_secrets_revet_exposure runtime-exposure "$lex" "$phys" || return 1
    if ! _project_secrets_pin_identity 65 "$phys"; then
        _project_secrets_fail runtime-identity "$phys"
        return 1
    fi
    readonly PROJECT_SECRETS_MSB_PATH="$phys"
    _PROJECT_SECRETS_MSB_FD=65
    return 0
}

# project_secrets_revalidate_runtime() — verify the pinned runtime still
# exists, still names the inode checked at resolution time, and is still
# rejected from the projects root and every registered descriptor domain
# (a hard link registered after pinning preserves the inode but lets
# project content rewrite the trusted binary in place). check_runtime
# runs this before any version process; callers run it before retaining
# any value so a swapped or newly exposed executable cannot run.
project_secrets_revalidate_runtime() {
    local lex
    if [[ -z "${PROJECT_SECRETS_MSB_PATH-}" ]]; then
        _project_secrets_fail runtime-identity ""
        return 1
    fi
    if ! _project_secrets_identity_matches "$_PROJECT_SECRETS_MSB_FD" "$PROJECT_SECRETS_MSB_PATH"; then
        _project_secrets_fail runtime-identity "$PROJECT_SECRETS_MSB_PATH"
        return 1
    fi
    lex="$(project_secrets_lexical_path "$PROJECT_SECRETS_MSB_PATH" "$PWD")"
    _project_secrets_revet_exposure runtime-exposure "$lex" "$PROJECT_SECRETS_MSB_PATH" || return 1
    return 0
}

# project_secrets_check_runtime() — run exactly one `--version` process
# through the pinned absolute executable and enforce the exact output
# contract: success status, empty stderr, stdout exactly `msb
# MAJOR.MINOR.PATCH` with zero or one trailing LF, decimal components
# without leading zeros, and a version in [0.6.12, 1.0.0). Callable only
# after a successful exposure preflight with the runtime and helpers
# pinned; a successful check marks runtime compatibility established,
# which metadata validation requires before it parses any source
# content, and rediscovery or any later registration clears the marker
# again. Every helper execution goes through the identity- and
# exposure-revalidating invocation gate; a version output file that
# vanishes before the read fails as runtime-output-vanished with staging
# removed; and the stdout read is bounded at 4097 characters, so
# NUL-bearing or oversized output is rejected without unbounded
# buffering; no valid version string approaches the bound.
project_secrets_check_runtime() {
    local out_file err_file raw content component status
    local version_re='^msb ([0-9]+)\.([0-9]+)\.([0-9]+)$'
    _project_secrets_require_unfrozen || return 1
    if [[ "$_PROJECT_SECRETS_EXPOSURE_STATE" != "ok" ]]; then
        _project_secrets_fail exposure-not-preflighted ""
        return 1
    fi
    if [[ -z "${PROJECT_SECRETS_MSB_PATH-}" ]]; then
        _project_secrets_fail runtime-unresolved ""
        return 1
    fi
    project_secrets_revalidate_runtime || return 1
    # Require both helpers before staging anything, so an already-replaced
    # helper is rejected before files exist that only it could remove.
    _project_secrets_require_helper PROJECT_SECRETS_HELPER_MKTEMP || return 1
    _project_secrets_require_helper PROJECT_SECRETS_HELPER_RM || return 1
    out_file="$(_project_secrets_invoke_helper PROJECT_SECRETS_HELPER_MKTEMP)" || return 1
    err_file="$(_project_secrets_invoke_helper PROJECT_SECRETS_HELPER_MKTEMP)" || {
        _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_RM -f "$out_file"
        return 1
    }
    "$PROJECT_SECRETS_MSB_PATH" --version >"$out_file" 2>"$err_file"
    status=$?
    if (( status != 0 )) || [[ -s "$err_file" ]]; then
        _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_RM -f "$out_file" "$err_file"
        _project_secrets_fail incompatible-runtime "$PROJECT_SECRETS_MSB_PATH"
        return 1
    fi
    if ! { exec 66<"$out_file"; } 2>/dev/null; then
        _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_RM -f "$out_file" "$err_file"
        _project_secrets_fail runtime-output-vanished "$PROJECT_SECRETS_MSB_PATH"
        return 1
    fi
    # Bounded read: a success status means a NUL byte was consumed as the
    # delimiter or the 4097-character bound was reached; both reject.
    if IFS= read -r -d '' -n 4097 raw <&66; then
        exec 66<&-
        _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_RM -f "$out_file" "$err_file"
        _project_secrets_fail malformed-runtime-version "$PROJECT_SECRETS_MSB_PATH"
        return 1
    fi
    exec 66<&-
    _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_RM -f "$out_file" "$err_file" || return 1
    content="$raw"
    if [[ "$content" == *$'\n' ]]; then
        content="${content%$'\n'}"
    fi
    if ! [[ "$content" =~ $version_re ]]; then
        _project_secrets_fail malformed-runtime-version "$PROJECT_SECRETS_MSB_PATH"
        return 1
    fi
    for component in "${BASH_REMATCH[@]:1:3}"; do
        if [[ "$component" == "0"* && "$component" != "0" ]]; then
            _project_secrets_fail malformed-runtime-version "$PROJECT_SECRETS_MSB_PATH"
            return 1
        fi
    done
    if (( BASH_REMATCH[1] > 0 )) \
        || (( BASH_REMATCH[2] < 6 )) \
        || (( BASH_REMATCH[2] == 6 && BASH_REMATCH[3] < 12 )); then
        _project_secrets_fail incompatible-runtime "$PROJECT_SECRETS_MSB_PATH"
        return 1
    fi
    _PROJECT_SECRETS_RUNTIME_STATE="ok"
    return 0
}

# _project_secrets_reject_staging_exposure(lex, phys) — fail with
# staging-exposure when the staging root lies inside or equals any
# registered exposure-descriptor domain (lexical or physical form) or
# the present-pair projects root. Only that direction is a collision:
# staging must stay outside every domain project or guest content can
# reach, while a projects root that merely lives somewhere under /tmp
# cannot write into a sibling staging directory.
_project_secrets_reject_staging_exposure() {
    local lex="$1" phys="$2" index domain relation
    local -a domains=()
    for (( index = 0; index < _PROJECT_SECRETS_DESCRIPTOR_COUNT; index++ )); do
        domains+=( "${_PROJECT_SECRETS_DESCRIPTOR_LEX[index]}" )
        domains+=( "${_PROJECT_SECRETS_DESCRIPTOR_PHYS[index]}" )
    done
    if [[ "$PROJECT_SECRETS_PAIR_STATE" == "present" ]]; then
        domains+=( "$PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL" )
        domains+=( "$PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL" )
    fi
    for domain in "${domains[@]}"; do
        if [[ "$lex" == "$domain" || "$phys" == "$domain" ]]; then
            _project_secrets_fail staging-exposure "$lex"
            return 1
        fi
        relation=0
        project_secrets_path_is_descendant "$domain" "$lex" || relation=$?
        if (( relation == 0 )); then
            _project_secrets_fail staging-exposure "$lex"
            return 1
        fi
        relation=0
        project_secrets_path_is_descendant "$domain" "$phys" || relation=$?
        if (( relation == 0 )); then
            _project_secrets_fail staging-exposure "$lex"
            return 1
        fi
    done
    return 0
}

# project_secrets_create_staging() — pre-freeze lifecycle step: create
# one mode-0700 staging directory under the fixed host /tmp through the
# pinned mktemp, predefine its mode-0600 policy path, reject the staging
# root from every registered exposure descriptor (and the projects root)
# before anything is created there, and record the directory, policy
# path, and physical identity readonly. The staging-root exposure
# self-check is one-shot: a descriptor registered between this call and
# validate_metadata is a launcher-ordering contract the freeze then
# locks in, not a condition create_staging re-checks. Callable only
# before a successful metadata validation: the descriptor registry is
# frozen by validation, so staging creation and its exposure self-check
# must happen before it, while validate_metadata still gates every later
# value use. The staging area contains no values; a second call fails
# with staging-already-created, and after the freeze the call fails with
# metadata-already-validated like every other state-mutating lifecycle
# call.
project_secrets_create_staging() {
    local staging_root="/tmp"
    local staging conf phys
    _project_secrets_require_unfrozen || return 1
    if [[ "$PROJECT_SECRETS_PAIR_STATE" != "present" ]]; then
        _project_secrets_fail staging-unavailable ""
        return 1
    fi
    if [[ -n "${PROJECT_SECRETS_STAGING_DIR-}" ]]; then
        _project_secrets_fail staging-already-created "$PROJECT_SECRETS_STAGING_DIR"
        return 1
    fi
    # Check the fixed staging root before creating anything inside it: a
    # descriptor covering /tmp (lexically or through a resolved alias)
    # means every possible staging directory is exposed, so nothing may
    # be created and no helper needs to run.
    phys="$(project_secrets_physical_path "$staging_root")" || {
        _project_secrets_fail staging-exposure "$staging_root"
        return 1
    }
    _project_secrets_reject_staging_exposure "$staging_root" "$phys" || return 1
    _project_secrets_require_helper PROJECT_SECRETS_HELPER_MKTEMP || return 1
    _project_secrets_require_helper PROJECT_SECRETS_HELPER_CHMOD || return 1
    _project_secrets_require_helper PROJECT_SECRETS_HELPER_RM || return 1
    staging="$(_project_secrets_invoke_helper PROJECT_SECRETS_HELPER_MKTEMP \
        -d "$staging_root/project-secrets.XXXXXXXXXX")" || return 1
    conf="$staging/secrets.conf"
    if ! { : > "$conf"; } 2>/dev/null; then
        _project_secrets_drop_staging "$staging"
        _project_secrets_fail staging-unavailable "$staging"
        return 1
    fi
    if ! _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_CHMOD \
        0700 "$staging"; then
        _project_secrets_drop_staging "$staging"
        return 1
    fi
    if ! _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_CHMOD \
        0600 "$conf"; then
        _project_secrets_drop_staging "$staging"
        return 1
    fi
    phys="$(project_secrets_physical_path "$staging")" || {
        _project_secrets_drop_staging "$staging"
        _project_secrets_fail staging-unavailable "$staging"
        return 1
    }
    readonly PROJECT_SECRETS_STAGING_DIR="$staging"
    readonly PROJECT_SECRETS_GENERATED_CONF="$conf"
    readonly _PROJECT_SECRETS_STAGING_PHYS="$phys"
    return 0
}

# _project_secrets_drop_staging(path) — best-effort removal of an
# incompletely created staging directory through the pinned removal
# helper; the caller has already reported its own diagnostic, and a
# removal failure additionally reports staging-cleanup with the leaked
# path instead of leaving the staging directory silently on disk.
_project_secrets_drop_staging() {
    if ! _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_RM -rf "$1"; then
        _project_secrets_fail staging-cleanup "$1"
    fi
    return 0
}


# project_secrets_note_ordinary_guest_name(name) — record one validated
# ordinary forwarded guest name in the private collision set synthetic
# source allocation skips. The name must be a valid identifier exactly
# as the launcher accepted it for forwarding; an invalid name fails
# with invalid-ordinary-name. Duplicate notes collapse; the set is
# deliberately mutable so the launcher may record names before or after
# metadata validation.
project_secrets_note_ordinary_guest_name() {
    local name="$1" existing
    if ! _project_secrets_is_valid_name "$name"; then
        _project_secrets_fail invalid-ordinary-name "$name"
        return 1
    fi
    for existing in "${_PROJECT_SECRETS_ORDINARY_NAMES[@]}"; do
        if [[ "$existing" == "$name" ]]; then
            return 0
        fi
    done
    _PROJECT_SECRETS_ORDINARY_NAMES+=( "$name" )
    return 0
}

# project_secrets_cleanup() — idempotent lifecycle cleanup: remove the
# launcher-owned staging directory through the pinned removal helper so
# the generated policy path never outlives the launch, then succeed.
# Repeated calls succeed (the removal tool tolerates an absent path),
# discovery state is untouched, and a launch without staging is a
# successful no-op. Removal happens on every lifecycle exit through the
# caller or the parent EXIT safety path.
project_secrets_cleanup() {
    if [[ -n "${PROJECT_SECRETS_STAGING_DIR-}" ]]; then
        _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_RM \
            -rf "$PROJECT_SECRETS_STAGING_DIR" || return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Strict value and policy grammars.

# Private reserved-name contract: the exact guest names the image
# entrypoint, the launcher, and the shell own, checked case-sensitively;
# the BASH, TAU_ENTRYPOINT_, and TAU_SANDBOX_SECRET_SOURCE_ prefixes are
# rejected separately. A reserved name can never be a project guest name
# in either source set.
_PROJECT_SECRETS_RESERVED_NAMES="HOME SHELL TERM COLORTERM USER LOGNAME PATH PYTHONUSERBASE NPM_CONFIG_PREFIX PIP_USER TAU_NO_UPDATE_CHECK TAU_SANDBOX_SHARED_CREDENTIALS BASH_ENV ENV IFS CDPATH GLOBIGNORE SHELLOPTS BASHOPTS LD_PRELOAD LD_LIBRARY_PATH PYTHONHOME PYTHONPATH NODE_OPTIONS PWD OLDPWD SHLVL _ EUID UID PPID BASHPID LINENO FUNCNAME GROUPS DIRSTACK PIPESTATUS RANDOM SECONDS HOSTNAME HOSTTYPE MACHTYPE OSTYPE OPTERR OPTIND OPTARG PS1 PS2 PS4 EPOCHSECONDS EPOCHREALTIME SRANDOM REPLY MAPFILE COPROC HISTCMD COLUMNS LINES"

# Private metadata state, rebuilt by every metadata validation.
# _PROJECT_SECRETS_VALUE_NAMES holds the value-source names in value-file
# order; the PARSED arrays hold one policy entry per secret in policy-file
# order; _PROJECT_SECRETS_SOURCE_TEXT carries one source's bytes from the
# loader to its parser and is unset before any grammar decision.
_PROJECT_SECRETS_VALUE_NAMES=()
_PROJECT_SECRETS_PARSED_NAMES=()
_PROJECT_SECRETS_PARSED_ALLOW=()
_PROJECT_SECRETS_PARSED_INJECT=()
_PROJECT_SECRETS_SOURCE_TEXT=""

# Private final-pass state. _PROJECT_SECRETS_ORDINARY_NAMES records the
# validated ordinary forwarded guest names for source-allocation
# collision avoidance; the SOURCE_VALUES and ALLOC_NAMES arrays are
# rebuilt by every prepare pass and exist only inside the clean child.
_PROJECT_SECRETS_ORDINARY_NAMES=()
_PROJECT_SECRETS_SOURCE_VALUES=()
_PROJECT_SECRETS_ALLOC_NAMES=()

# _project_secrets_line_error(class, path, lineno) — report a
# class/path/line diagnostic on stderr for one rejected source line.
# Never emits the line, the name, or the value.
_project_secrets_line_error() {
    printf 'project-secrets: %s: %s: line %s\n' "$1" "$2" "$3" >&2
    return 0
}

# _project_secrets_is_valid_name(name) — 0 when name is nonempty,
# begins with an ASCII letter or underscore in column one, and contains
# only ASCII letters, digits, and underscores.
_project_secrets_is_valid_name() {
    local name="$1"
    [[ -n "$name" ]] || return 1
    [[ "$name" == [A-Za-z_]* ]] || return 1
    [[ "$name" != *[!A-Za-z0-9_]* ]]
}

# _project_secrets_is_reserved_name(name) — 0 when name is one of the
# exact reserved names or begins one of the reserved prefixes. Names
# compare case-sensitively, so only exact reserved spellings reject.
_project_secrets_is_reserved_name() {
    local name="$1"
    case "$name" in
        BASH* | TAU_ENTRYPOINT_* | TAU_SANDBOX_SECRET_SOURCE_*) return 0 ;;
    esac
    [[ " $_PROJECT_SECRETS_RESERVED_NAMES " == *" $name "* ]]
}

# _project_secrets_lowercase(text) — print text with every ASCII
# uppercase letter mapped to lowercase, using builtins only. Destination
# canonicalization has no locale or case-modification feature available
# under the Bash 4.0 builtins-only contract, so the mapping is explicit.
_project_secrets_lowercase() {
    local input="$1" output="" index char prefix
    local upper="ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    local lower="abcdefghijklmnopqrstuvwxyz"
    for (( index = 0; index < ${#input}; index++ )); do
        char="${input:index:1}"
        prefix="${upper%%"$char"*}"
        if (( ${#prefix} < 26 )); then
            char="${lower:${#prefix}:1}"
        fi
        output+="$char"
    done
    printf '%s' "$output"
}

# _project_secrets_valid_host(host) — 0 when host is two or more valid
# ASCII DNS labels and is not an all-numeric IP literal shape. Each label
# is 1-63 characters, begins and ends with an ASCII letter or digit, and
# contains only letters, digits, and internal hyphens; leading, trailing,
# and empty labels reject.
_project_secrets_valid_host() {
    local host="$1" rest="$1" label count=0 non_numeric_seen=0
    [[ "$host" != "."* && "$host" != *"." ]] || return 1
    while [[ -n "$rest" ]]; do
        label="${rest%%.*}"
        if [[ "$rest" == *.* ]]; then
            rest="${rest#*.}"
        else
            rest=""
        fi
        [[ -n "$label" ]] || return 1
        [[ "$label" != *[!A-Za-z0-9-]* ]] || return 1
        [[ "$label" != -* && "$label" != *- ]] || return 1
        (( ${#label} <= 63 )) || return 1
        [[ "$label" != *[!0-9]* ]] || non_numeric_seen=1
        count=$(( count + 1 ))
    done
    (( count >= 2 )) || return 1
    (( non_numeric_seen == 1 )) || return 1
    return 0
}

# _project_secrets_destination(scalar) — validate one allow scalar and
# set _PROJECT_SECRETS_CANON to its canonical lowercase destination. The
# scalar is either unquoted or one matching pair of single or double
# quotes containing the complete scalar with no escapes; after
# unquoting, an exact destination needs two or more labels and at most
# 253 characters, a wildcard is "*." plus such a suffix (at most 255
# characters in total). Empty scalars, "*", IP literals, ports, and every
# other character reject.
_project_secrets_destination() {
    local scalar="$1" body host
    case "$scalar" in
        \'*)
            [[ ${#scalar} -ge 2 && "$scalar" == \'*\' ]] || return 1
            body="${scalar#\'}"
            body="${body%\'}"
            ;;
        \"*)
            [[ ${#scalar} -ge 2 && "$scalar" == \"*\" ]] || return 1
            body="${scalar#\"}"
            body="${body%\"}"
            ;;
        *)
            body="$scalar"
            ;;
    esac
    [[ -n "$body" ]] || return 1
    if [[ "$body" == "*."* ]]; then
        host="${body#*.}"
    else
        host="$body"
    fi
    [[ ${#host} -le 253 ]] || return 1
    _project_secrets_valid_host "$host" || return 1
    _PROJECT_SECRETS_CANON="$(_project_secrets_lowercase "$body")"
    return 0
}

# _project_secrets_load_source(class, path) — read one source file
# entirely into _PROJECT_SECRETS_SOURCE_TEXT with builtins only. A NUL
# byte is detected by the delimiter read and fails with the class, path,
# and the line the NUL lies on; the NUL itself is never stored. Accepted
# deviation from the plan's "never stores unsupported bytes in Bash
# variables": the complete file text, rejected bytes included, is held
# transiently in shell variables until the per-line rejection fires, and
# nothing rejected survives the call. A source that cannot be opened
# fails as invalid-secret-source.
_project_secrets_load_source() {
    local class="$1" path="$2" raw stripped lineno
    _PROJECT_SECRETS_SOURCE_TEXT=""
    if ! { exec 67<"$path"; } 2>/dev/null; then
        _project_secrets_fail invalid-secret-source "$path"
        return 1
    fi
    raw=""
    if IFS= read -r -d '' raw <&67; then
        exec 67<&-
        stripped="${raw//$'\n'/}"
        lineno=$(( ${#raw} - ${#stripped} + 1 ))
        _project_secrets_line_error "$class-invalid-byte" "$path" "$lineno"
        return 1
    fi
    exec 67<&-
    _PROJECT_SECRETS_SOURCE_TEXT="$raw"
    return 0
}

# _project_secrets_parse_values(retain) — validate the value source
# grammar and collect names into _PROJECT_SECRETS_VALUE_NAMES in
# value-file order. With a nonzero retain argument the literal value
# text of every entry is kept in _PROJECT_SECRETS_SOURCE_VALUES for the
# clean child's final pass; the metadata pass passes zero, leaving that
# array empty, so no value text survives validation. Lines are printable
# ASCII only (CR is
# accepted solely as the CRLF terminator); blank, space-only, and
# full-line comment lines are ignored; every other line is NAME=VALUE
# with the name in column one, and all characters after the first
# equals sign are literal data that is discarded once checked.
_project_secrets_parse_values() {
    local retain="$1"
    local path="$PROJECT_SECRETS_VALUES_LEXICAL"
    local raw line name value lineno nospace existing had_lf
    _PROJECT_SECRETS_VALUE_NAMES=()
    _PROJECT_SECRETS_SOURCE_VALUES=()
    _project_secrets_load_source value "$path" || return 1
    raw="$_PROJECT_SECRETS_SOURCE_TEXT"
    unset _PROJECT_SECRETS_SOURCE_TEXT
    lineno=0
    while [[ -n "$raw" ]]; do
        if [[ "$raw" == *$'\n'* ]]; then
            line="${raw%%$'\n'*}"
            raw="${raw#*$'\n'}"
            had_lf=1
        else
            line="$raw"
            raw=""
            had_lf=0
        fi
        lineno=$(( lineno + 1 ))
        if (( had_lf )) && [[ "$line" == *$'\r' ]]; then
            line="${line%$'\r'}"
        fi
        if [[ "$line" == *[!\ -~]* ]]; then
            _project_secrets_line_error value-invalid-byte "$path" "$lineno"
            return 1
        fi
        nospace="${line// /}"
        if [[ -z "$nospace" ]]; then
            continue
        fi
        if [[ "$nospace" == "#"* ]]; then
            continue
        fi
        if [[ "$line" != *=* ]]; then
            _project_secrets_line_error value-malformed-entry "$path" "$lineno"
            return 1
        fi
        name="${line%%=*}"
        value="${line#*=}"
        if ! _project_secrets_is_valid_name "$name"; then
            _project_secrets_line_error value-malformed-entry "$path" "$lineno"
            return 1
        fi
        if _project_secrets_is_reserved_name "$name"; then
            _project_secrets_line_error reserved-name "$path" "$lineno"
            return 1
        fi
        if [[ -z "$value" ]]; then
            _project_secrets_line_error value-empty-value "$path" "$lineno"
            return 1
        fi
        for existing in "${_PROJECT_SECRETS_VALUE_NAMES[@]}"; do
            if [[ "$existing" == "$name" ]]; then
                _project_secrets_line_error value-duplicate-name "$path" "$lineno"
                return 1
            fi
        done
        _PROJECT_SECRETS_VALUE_NAMES+=( "$name" )
        if (( retain )); then
            _PROJECT_SECRETS_SOURCE_VALUES+=( "$value" )
        fi
    done
    return 0
}

# _project_secrets_parse_policy() — validate the restricted policy
# grammar and collect one (name, allow, inject) triple per secret into
# the PARSED arrays in policy-file order. The grammar is a line state
# machine: a NAME: header in column one, exactly one required "  allow:"
# field with one or more "    - destination" items, an optional
# "  inject:" field with one or more "    - location" items from the
# exact supported set, blank/space-only/comment lines between elements,
# and nothing else — no trailing whitespace, field reordering, inline
# comments, or unknown syntax. Destinations canonicalize to lowercase
# before duplicate comparison; names, fields, and injection values
# compare case-sensitively.
_project_secrets_parse_policy() {
    local path="$PROJECT_SECRETS_POLICY_LEXICAL"
    local raw line name field rest scalar canon lineno nospace existing had_lf
    local state="top" current_name="" current_allow="" current_inject=""
    _PROJECT_SECRETS_PARSED_NAMES=()
    _PROJECT_SECRETS_PARSED_ALLOW=()
    _PROJECT_SECRETS_PARSED_INJECT=()
    _project_secrets_load_source policy "$path" || return 1
    raw="$_PROJECT_SECRETS_SOURCE_TEXT"
    unset _PROJECT_SECRETS_SOURCE_TEXT
    lineno=0
    while [[ -n "$raw" ]]; do
        if [[ "$raw" == *$'\n'* ]]; then
            line="${raw%%$'\n'*}"
            raw="${raw#*$'\n'}"
            had_lf=1
        else
            line="$raw"
            raw=""
            had_lf=0
        fi
        lineno=$(( lineno + 1 ))
        if (( had_lf )) && [[ "$line" == *$'\r' ]]; then
            line="${line%$'\r'}"
        fi
        if [[ "$line" == *[!\ -~]* ]]; then
            _project_secrets_line_error policy-invalid-byte "$path" "$lineno"
            return 1
        fi
        nospace="${line// /}"
        if [[ -z "$nospace" ]]; then
            continue
        fi
        if [[ "$nospace" == "#"* ]]; then
            continue
        fi
        if [[ "$line" == "    "* ]]; then
            rest="${line#    }"
            if [[ "$rest" != "- "* ]]; then
                _project_secrets_line_error policy-syntax "$path" "$lineno"
                return 1
            fi
            scalar="${rest#- }"
            case "$state" in
                allow)
                    if ! _project_secrets_destination "$scalar"; then
                        _project_secrets_line_error policy-destination "$path" "$lineno"
                        return 1
                    fi
                    canon="$_PROJECT_SECRETS_CANON"
                    if [[ " $current_allow " == *" $canon "* ]]; then
                        _project_secrets_line_error policy-duplicate "$path" "$lineno"
                        return 1
                    fi
                    current_allow="$current_allow $canon"
                    ;;
                inject)
                    case "$scalar" in
                        headers | basic_auth | query_params) ;;
                        *)
                            _project_secrets_line_error policy-syntax "$path" "$lineno"
                            return 1
                            ;;
                    esac
                    if [[ " $current_inject " == *" $scalar "* ]]; then
                        _project_secrets_line_error policy-duplicate "$path" "$lineno"
                        return 1
                    fi
                    current_inject="$current_inject $scalar"
                    ;;
                *)
                    _project_secrets_line_error policy-syntax "$path" "$lineno"
                    return 1
                    ;;
            esac
        elif [[ "$line" == "  "* ]]; then
            field="${line#  }"
            case "$field" in
                "allow:")
                    case "$state" in
                        field) state="allow" ;;
                        allow)
                            _project_secrets_line_error policy-duplicate "$path" "$lineno"
                            return 1
                            ;;
                        *)
                            _project_secrets_line_error policy-syntax "$path" "$lineno"
                            return 1
                            ;;
                    esac
                    ;;
                "inject:")
                    case "$state" in
                        allow) state="inject" ;;
                        inject)
                            _project_secrets_line_error policy-duplicate "$path" "$lineno"
                            return 1
                            ;;
                        *)
                            _project_secrets_line_error policy-syntax "$path" "$lineno"
                            return 1
                            ;;
                    esac
                    ;;
                *)
                    _project_secrets_line_error policy-syntax "$path" "$lineno"
                    return 1
                    ;;
            esac
        else
            if [[ "$line" != *":" ]]; then
                _project_secrets_line_error policy-syntax "$path" "$lineno"
                return 1
            fi
            name="${line%:}"
            if ! _project_secrets_is_valid_name "$name"; then
                _project_secrets_line_error policy-syntax "$path" "$lineno"
                return 1
            fi
            if _project_secrets_is_reserved_name "$name"; then
                _project_secrets_line_error reserved-name "$path" "$lineno"
                return 1
            fi
            for existing in "${_PROJECT_SECRETS_PARSED_NAMES[@]}" "$current_name"; do
                if [[ "$existing" == "$name" ]]; then
                    _project_secrets_line_error policy-duplicate "$path" "$lineno"
                    return 1
                fi
            done
            case "$state" in
                field)
                    _project_secrets_line_error policy-syntax "$path" "$lineno"
                    return 1
                    ;;
                allow)
                    if [[ -z "$current_allow" ]]; then
                        _project_secrets_line_error policy-syntax "$path" "$lineno"
                        return 1
                    fi
                    ;;
                inject)
                    if [[ -z "$current_inject" ]]; then
                        _project_secrets_line_error policy-syntax "$path" "$lineno"
                        return 1
                    fi
                    ;;
            esac
            if [[ -n "$current_name" ]]; then
                _PROJECT_SECRETS_PARSED_NAMES+=( "$current_name" )
                _PROJECT_SECRETS_PARSED_ALLOW+=( "${current_allow# }" )
                _PROJECT_SECRETS_PARSED_INJECT+=( "${current_inject# }" )
            fi
            current_name="$name"
            current_allow=""
            current_inject=""
            state="field"
        fi
    done
    case "$state" in
        field)
            _project_secrets_line_error policy-syntax "$path" "$lineno"
            return 1
            ;;
        allow)
            if [[ -z "$current_allow" ]]; then
                _project_secrets_line_error policy-syntax "$path" "$lineno"
                return 1
            fi
            ;;
        inject)
            if [[ -z "$current_inject" ]]; then
                _project_secrets_line_error policy-syntax "$path" "$lineno"
                return 1
            fi
            ;;
    esac
    if [[ -n "$current_name" ]]; then
        _PROJECT_SECRETS_PARSED_NAMES+=( "$current_name" )
        _PROJECT_SECRETS_PARSED_ALLOW+=( "${current_allow# }" )
        _PROJECT_SECRETS_PARSED_INJECT+=( "${current_inject# }" )
    fi
    return 0
}

# _project_secrets_harden_state() — freeze the library after a successful
# metadata validation: every public path and pair field, the exposure and
# runtime markers, the exposure-descriptor registry and count, the helper
# pinning tables and marker, the pinned runtime descriptor, and the
# validated name and policy arrays (already readonly where
# validate_metadata set them, as are the pinned helper and runtime paths)
# become readonly, and every library function is declared readonly with
# `readonly -f`, so trusted ordinary configuration running before value
# retention can replace none of them. The descriptor and pin bookkeeping
# is what post-freeze revalidation re-vets executables against, so a
# zeroed count or redirected pin could not let a hard-linked executable
# escape exposure re-vetting. Bash 4.0 supports readonly functions —
# redefining or unsetting one fails — so no fallback mechanism is
# required.
_project_secrets_harden_state() {
    local line name
    readonly PROJECT_SECRETS_PAIR_STATE
    readonly PROJECT_SECRETS_PROJECTS_ROOT_LEXICAL
    readonly PROJECT_SECRETS_PROJECTS_ROOT_PHYSICAL
    readonly PROJECT_SECRETS_DIR_LEXICAL
    readonly PROJECT_SECRETS_DIR_PHYSICAL
    readonly PROJECT_SECRETS_VALUES_LEXICAL
    readonly PROJECT_SECRETS_VALUES_PHYSICAL
    readonly PROJECT_SECRETS_POLICY_LEXICAL
    readonly PROJECT_SECRETS_POLICY_PHYSICAL
    readonly _PROJECT_SECRETS_EXPOSURE_STATE
    readonly _PROJECT_SECRETS_RUNTIME_STATE
    readonly _PROJECT_SECRETS_DESCRIPTOR_KINDS
    readonly _PROJECT_SECRETS_DESCRIPTOR_LEX
    readonly _PROJECT_SECRETS_DESCRIPTOR_PHYS
    readonly _PROJECT_SECRETS_DESCRIPTOR_COUNT
    readonly _PROJECT_SECRETS_HELPER_NAMES
    readonly _PROJECT_SECRETS_HELPER_FIELDS
    readonly _PROJECT_SECRETS_HELPER_FDS
    readonly _PROJECT_SECRETS_HELPERS_PINNED
    readonly _PROJECT_SECRETS_MSB_FD
    readonly PROJECT_SECRETS_STAGING_DIR
    readonly PROJECT_SECRETS_GENERATED_CONF
    readonly _PROJECT_SECRETS_STAGING_PHYS
    while read -r line; do
        name="${line##* }"
        case "$name" in
            project_secrets_* | _project_secrets_*)
                readonly -f "$name"
                ;;
        esac
    done < <(declare -F)
}

# project_secrets_validate_metadata() — validate the strict value and
# policy grammars of the present pair without retaining real values,
# reachable only after a successful exposure preflight AND an established
# runtime compatibility (a successful project_secrets_check_runtime): no
# source byte is parsed before the runtime version check has succeeded,
# so an incompatible runtime always takes precedence over empty or
# malformed content. Every byte, line, name, and policy element is
# checked, the two name sets must match exactly and be nonempty, and only
# then is the readonly public PROJECT_SECRET_NAMES array (value-file
# order) plus the private effective-policy arrays set. An omitted inject
# field becomes the effective headers-only policy. On success the whole
# library is frozen readonly (see _project_secrets_harden_state), so a
# second call — directly or after any attempted rediscovery — fails with
# the metadata-already-validated class instead of re-setting readonly
# state. Requires LC_ALL=C so byte and character-class matching is by
# byte.
project_secrets_validate_metadata() {
    local LC_ALL=C
    local index name policy_name found allow inject
    local -a effective_allow=() effective_inject=()
    _project_secrets_require_unfrozen || return 1
    if [[ "$PROJECT_SECRETS_PAIR_STATE" != "present" ]]; then
        _project_secrets_fail exposure-not-preflighted ""
        return 1
    fi
    if [[ "$_PROJECT_SECRETS_EXPOSURE_STATE" != "ok" ]]; then
        _project_secrets_fail exposure-not-preflighted ""
        return 1
    fi
    if [[ "$_PROJECT_SECRETS_RUNTIME_STATE" != "ok" ]]; then
        _project_secrets_fail runtime-not-checked ""
        return 1
    fi
    _project_secrets_parse_values 0 || return 1
    _project_secrets_parse_policy || return 1
    if (( ${#_PROJECT_SECRETS_VALUE_NAMES[@]} == 0 )); then
        _project_secrets_fail policy-empty "$PROJECT_SECRETS_POLICY_LEXICAL"
        return 1
    fi
    if (( ${#_PROJECT_SECRETS_VALUE_NAMES[@]} != ${#_PROJECT_SECRETS_PARSED_NAMES[@]} )); then
        _project_secrets_fail policy-name-mismatch "$PROJECT_SECRETS_POLICY_LEXICAL"
        return 1
    fi
    for name in "${_PROJECT_SECRETS_VALUE_NAMES[@]}"; do
        found=0
        for policy_name in "${_PROJECT_SECRETS_PARSED_NAMES[@]}"; do
            if [[ "$policy_name" == "$name" ]]; then
                found=1
                break
            fi
        done
        if (( found == 0 )); then
            _project_secrets_fail policy-name-mismatch "$PROJECT_SECRETS_POLICY_LEXICAL"
            return 1
        fi
    done
    for name in "${_PROJECT_SECRETS_VALUE_NAMES[@]}"; do
        for (( index = 0; index < ${#_PROJECT_SECRETS_PARSED_NAMES[@]}; index++ )); do
            if [[ "${_PROJECT_SECRETS_PARSED_NAMES[index]}" == "$name" ]]; then
                allow="${_PROJECT_SECRETS_PARSED_ALLOW[index]}"
                inject="${_PROJECT_SECRETS_PARSED_INJECT[index]}"
                [[ -n "$inject" ]] || inject="headers"
                effective_allow+=( "$allow" )
                effective_inject+=( "$inject" )
            fi
        done
    done
    PROJECT_SECRET_NAMES=( "${_PROJECT_SECRETS_VALUE_NAMES[@]}" )
    _PROJECT_SECRETS_POLICY_ALLOW=( "${effective_allow[@]}" )
    _PROJECT_SECRETS_POLICY_INJECT=( "${effective_inject[@]}" )
    readonly PROJECT_SECRET_NAMES
    readonly _PROJECT_SECRETS_POLICY_ALLOW
    readonly _PROJECT_SECRETS_POLICY_INJECT
    _project_secrets_harden_state
    # Enter POSIX mode before trusted ordinary configuration can run:
    # the special builtins (set, trap, unset, export, exec, eval, ...)
    # then precede any function of the same name, so the isolated
    # invocation's hardening steps cannot be intercepted by a shadow.
    set -o posix
    return 0
}

# project_secrets_is_guest_name(name) — 0 exactly when name is one of
# the validated project guest names; emits nothing, so callers can
# probe names without any observable side effect.
project_secrets_is_guest_name() {
    local name="$1" guest
    for guest in "${PROJECT_SECRET_NAMES[@]}"; do
        if [[ "$guest" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

# project_secrets_validate_env_source(env_file) — vet one candidate
# TAU_ENV_FILE before it is sourced: with a present pair, the file's
# normalized lexical path, resolved physical path, and filesystem
# identity must all differ from the value source, so lexical aliases,
# symlinks, and hard links reject; identity uncertainty fails closed.
# An absent file stays a valid ordinary source, and a no-pair launch has
# no value source to alias.
project_secrets_validate_env_source() {
    local env_file="$1" lex phys relation
    if [[ "$PROJECT_SECRETS_PAIR_STATE" != "present" ]]; then
        return 0
    fi
    if [[ ! -e "$env_file" && ! -L "$env_file" ]]; then
        return 0
    fi
    lex="$(project_secrets_lexical_path "$env_file" "$PWD")"
    if [[ "$lex" == "$PROJECT_SECRETS_VALUES_LEXICAL" ]]; then
        _project_secrets_fail env-source-alias "$lex"
        return 1
    fi
    phys="$(project_secrets_physical_path "$lex")" || {
        _project_secrets_fail env-source-identity-uncertain "$lex"
        return 1
    }
    if [[ "$phys" == "$PROJECT_SECRETS_VALUES_PHYSICAL" ]]; then
        _project_secrets_fail env-source-alias "$phys"
        return 1
    fi
    relation=0
    project_secrets_paths_identical "$phys" "$PROJECT_SECRETS_VALUES_PHYSICAL" || relation=$?
    if (( relation == 0 )); then
        _project_secrets_fail env-source-alias "$phys"
        return 1
    fi
    if (( relation == 2 )); then
        _project_secrets_fail env-source-identity-uncertain "$phys"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Isolated final invocation.

# _project_secrets_sanitize_shell() — harden one subshell before any
# value work: clear xtrace, verbose, and the functrace/errtrace
# options, reset the DEBUG/RETURN/ERR/EXIT traps, and remove every
# non-library shell function, so instrumentation inherited from trusted
# ordinary configuration can neither observe nor block the final value
# pass. Requires the POSIX mode project_secrets_validate_metadata
# establishes: the POSIX special builtins (set, trap, unset) then
# precede any same-named function, the first steps cannot be
# intercepted, and unshadowing declare makes the enumeration itself
# trustworthy. Runs only inside a subshell — on purpose without local
# variables, which are function calls — leaving the parent's options,
# traps, and functions untouched.
_project_secrets_sanitize_shell() {
    set +x +v +E +T +e +u
    trap - DEBUG RETURN ERR EXIT
    unset -f declare
    _PROJECT_SECRETS_SANITIZE_TEXT="$(declare -F)"
    while [[ -n "$_PROJECT_SECRETS_SANITIZE_TEXT" ]]; do
        _PROJECT_SECRETS_SANITIZE_LINE="${_PROJECT_SECRETS_SANITIZE_TEXT%%$'\n'*}"
        if [[ "$_PROJECT_SECRETS_SANITIZE_TEXT" == *$'\n'* ]]; then
            _PROJECT_SECRETS_SANITIZE_TEXT="${_PROJECT_SECRETS_SANITIZE_TEXT#*$'\n'}"
        else
            _PROJECT_SECRETS_SANITIZE_TEXT=""
        fi
        _PROJECT_SECRETS_SANITIZE_NAME="${_PROJECT_SECRETS_SANITIZE_LINE##* }"
        if [[ -n "$_PROJECT_SECRETS_SANITIZE_NAME" \
            && "$_PROJECT_SECRETS_SANITIZE_NAME" == [A-Za-z_]* \
            && "$_PROJECT_SECRETS_SANITIZE_NAME" != *[!A-Za-z0-9_]* ]]; then
            case "$_PROJECT_SECRETS_SANITIZE_NAME" in
                project_secrets_* | _project_secrets_*) ;;
                *) unset -f "$_PROJECT_SECRETS_SANITIZE_NAME" 2>/dev/null ;;
            esac
        fi
    done
    unset _PROJECT_SECRETS_SANITIZE_TEXT
    unset _PROJECT_SECRETS_SANITIZE_LINE
    unset _PROJECT_SECRETS_SANITIZE_NAME
    return 0
}

# _project_secrets_exit_cleanup() — shared cleanup path: run the library
# cleanup inside a sanitized subshell so instrumentation left behind by
# trusted ordinary configuration cannot block the pinned removal, while
# the parent's own functions and options stay untouched. Idempotent; it
# never changes the shell's exit status. project_secrets_exec_runtime
# calls it directly after the isolated child returns, and the parent
# EXIT safety path runs it through _project_secrets_exit_handler.
_project_secrets_exit_cleanup() {
    (
        _project_secrets_sanitize_shell
        project_secrets_cleanup
    )
    return 0
}

# Private EXIT-trap composition state: the EXIT-trap action
# project_secrets_exec_runtime found installed before it armed the
# library exit handler, empty when the launcher had none. Only the exit
# handler reads it, and a repeated isolated invocation never overwrites
# it with the library's own handler.
_PROJECT_SECRETS_PRIOR_EXIT=""

# _project_secrets_exit_handler() — parent EXIT safety path: run the
# shared library cleanup, then run the EXIT-trap action the launcher had
# installed before project_secrets_exec_runtime armed this handler, so a
# launcher-owned exit cleanup composes with the library's instead of
# being replaced. The prior action is kept exactly as `trap -p` quoted
# it and executed through a double eval, which reproduces any quoting
# the original trap carried. Never changes the shell's exit status.
_project_secrets_exit_handler() {
    _project_secrets_exit_cleanup
    if [[ -n "${_PROJECT_SECRETS_PRIOR_EXIT-}" ]]; then
        eval "eval $_PROJECT_SECRETS_PRIOR_EXIT"
    fi
    return 0
}

# _project_secrets_source_candidate_free(candidate) — 0 when the
# synthetic source candidate collides with nothing: not present in the
# process environment (or shell state), not an ordinary forwarded
# guest name, not a project guest name, and not already allocated to an
# earlier secret in this pass.
_project_secrets_source_candidate_free() {
    local candidate="$1" existing
    if declare -p "$candidate" >/dev/null 2>&1; then
        return 1
    fi
    for existing in "${_PROJECT_SECRETS_ORDINARY_NAMES[@]}"; do
        if [[ "$existing" == "$candidate" ]]; then
            return 1
        fi
    done
    for existing in "${PROJECT_SECRET_NAMES[@]}"; do
        if [[ "$existing" == "$candidate" ]]; then
            return 1
        fi
    done
    for existing in "${_PROJECT_SECRETS_ALLOC_NAMES[@]}"; do
        if [[ "$existing" == "$candidate" ]]; then
            return 1
        fi
    done
    return 0
}

# _project_secrets_build_runtime_argv(msb_argv...) — validate the
# caller's runtime argument sequence (whose first element is exactly
# run, gated by the caller) and assemble the exact argv the runtime is
# executed with: runtime options end at the first exact -- separator, a
# caller-supplied --secret-conf or --secret-conf=PATH in that section
# rejects with runtime-secret-conf, a missing separator rejects with
# runtime-arguments, and every guest element after the separator is
# preserved byte-for-byte — including literal --secret-conf spellings.
# The single generated --secret-conf PROJECT_SECRETS_GENERATED_CONF is
# inserted immediately after run and nowhere else. Fills the private
# _PROJECT_SECRETS_RUNTIME_ARGV array.
_project_secrets_build_runtime_argv() {
    local arg separator_seen=0
    local -a runtime_opts=() guest_args=()
    shift
    for arg in "$@"; do
        if (( separator_seen == 0 )) && [[ "$arg" == "--" ]]; then
            separator_seen=1
            continue
        fi
        if (( separator_seen == 0 )); then
            case "$arg" in
                --secret-conf | --secret-conf=*)
                    _project_secrets_fail runtime-secret-conf "$arg"
                    return 1
                    ;;
            esac
            runtime_opts+=( "$arg" )
        else
            guest_args+=( "$arg" )
        fi
    done
    if (( separator_seen == 0 )); then
        _project_secrets_fail runtime-arguments ""
        return 1
    fi
    _PROJECT_SECRETS_RUNTIME_ARGV=( run --secret-conf "$PROJECT_SECRETS_GENERATED_CONF" )
    _PROJECT_SECRETS_RUNTIME_ARGV+=( "${runtime_opts[@]}" )
    _PROJECT_SECRETS_RUNTIME_ARGV+=( -- )
    _PROJECT_SECRETS_RUNTIME_ARGV+=( "${guest_args[@]}" )
    return 0
}

# project_secrets_prepare_runtime() — private clean-child final pass,
# reachable only from project_secrets_exec_runtime's hardened subshell
# (the private child marker is set there and nowhere else) and only
# while the POSIX-mode invariant holds. Revalidates the pinned runtime
# identity, revalidates the staging directory's identity and re-asserts
# its 0700 mode and the policy path's 0600 mode through the pinned
# chmod, reparses the value source under the full grammar and requires
# its names to still match the frozen PROJECT_SECRET_NAMES exactly,
# allocates one collision-free TAU_SANDBOX_SECRET_SOURCE_<n> name per
# secret in value-file order, writes the exact generated policy, and
# exports every literal value under its synthetic name. Values live
# only in this subshell's environment, ready for the exec that follows.
# The pass pins IFS to a single space for its whole body, so an ambient
# IFS mutated by trusted ordinary configuration cannot split the frozen
# space-separated policy strings into fragments when the generation
# loops iterate them.
project_secrets_prepare_runtime() {
    local LC_ALL=C
    local IFS=' '
    local dir conf phys index name source_name candidate dest loc policy
    if [[ "${_PROJECT_SECRETS_CHILD-}" != "1" ]]; then
        _project_secrets_fail runtime-context ""
        return 1
    fi
    if [[ ":$SHELLOPTS:" != *":posix:"* ]]; then
        _project_secrets_fail runtime-context ""
        return 1
    fi
    project_secrets_revalidate_runtime || return 1
    dir="$PROJECT_SECRETS_STAGING_DIR"
    conf="$PROJECT_SECRETS_GENERATED_CONF"
    if [[ ! -d "$dir" || -L "$dir" || ! -O "$dir" ]]; then
        _project_secrets_fail staging-identity "$dir"
        return 1
    fi
    phys="$(project_secrets_physical_path "$dir")" || {
        _project_secrets_fail staging-identity "$dir"
        return 1
    }
    if [[ "$phys" != "$_PROJECT_SECRETS_STAGING_PHYS" ]]; then
        _project_secrets_fail staging-identity "$dir"
        return 1
    fi
    if [[ ! -f "$conf" || -L "$conf" ]]; then
        _project_secrets_fail staging-identity "$conf"
        return 1
    fi
    _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_CHMOD 0700 "$dir" \
        || return 1
    _project_secrets_invoke_helper PROJECT_SECRETS_HELPER_CHMOD 0600 "$conf" \
        || return 1
    _project_secrets_parse_values 1 || return 1
    if (( ${#_PROJECT_SECRETS_VALUE_NAMES[@]} != ${#PROJECT_SECRET_NAMES[@]} )); then
        _project_secrets_fail value-source-changed "$PROJECT_SECRETS_VALUES_LEXICAL"
        return 1
    fi
    for (( index = 0; index < ${#_PROJECT_SECRETS_VALUE_NAMES[@]}; index++ )); do
        if [[ "${_PROJECT_SECRETS_VALUE_NAMES[index]}" != "${PROJECT_SECRET_NAMES[index]}" ]]; then
            _project_secrets_fail value-source-changed "$PROJECT_SECRETS_VALUES_LEXICAL"
            return 1
        fi
    done
    _PROJECT_SECRETS_ALLOC_NAMES=()
    for (( index = 0; index < ${#PROJECT_SECRET_NAMES[@]}; index++ )); do
        candidate="TAU_SANDBOX_SECRET_SOURCE_0"
        while ! _project_secrets_source_candidate_free "$candidate"; do
            candidate="TAU_SANDBOX_SECRET_SOURCE_$(( ${candidate##*_} + 1 ))"
        done
        _PROJECT_SECRETS_ALLOC_NAMES+=( "$candidate" )
    done
    policy=""
    for (( index = 0; index < ${#PROJECT_SECRET_NAMES[@]}; index++ )); do
        name="${PROJECT_SECRET_NAMES[index]}"
        source_name="${_PROJECT_SECRETS_ALLOC_NAMES[index]}"
        policy+="\"${name}\":"$'\n'
        policy+="  value: \"\${${source_name}}\""$'\n'
        policy+="  allow:"$'\n'
        for dest in ${_PROJECT_SECRETS_POLICY_ALLOW[index]}; do
            policy+="    - \"${dest}\""$'\n'
        done
        policy+="  inject:"$'\n'
        for loc in ${_PROJECT_SECRETS_POLICY_INJECT[index]}; do
            policy+="    - ${loc}"$'\n'
        done
    done
    if ! builtin printf '%s' "$policy" > "$conf"; then
        _project_secrets_fail staging-write "$conf"
        return 1
    fi
    unset policy
    for (( index = 0; index < ${#PROJECT_SECRET_NAMES[@]}; index++ )); do
        export "${_PROJECT_SECRETS_ALLOC_NAMES[index]}=${_PROJECT_SECRETS_SOURCE_VALUES[index]}"
    done
    return 0
}

# project_secrets_exec_runtime(msb_argv...) — the isolated final
# invocation. The first argument must be exactly run; with a validated
# present pair, staged policy, and the POSIX-mode invariant, the parent
# composes with any launcher-owned EXIT trap (capturing the currently
# installed action and running it, after the library cleanup, from the
# exit handler), launches one synchronous subshell, and returns the
# exact runtime status after removing staging through the pinned
# removal tool. The child hardens itself (see
# _project_secrets_sanitize_shell), builds the exact argv with exactly
# one --secret-conf inserted immediately after run, prepares the values
# and generated policy, and execs the pinned runtime; no child trap
# owns cleanup, so exec replaces only the clean child. The parent path
# deliberately uses only POSIX special builtins and pure syntax — no
# local variables and no shadowable builtin — so trusted ordinary
# configuration left behind in the parent cannot redirect or observe
# it, and the diagnostic helper's printf, if shadowed, can only lose a
# message, never a value.
project_secrets_exec_runtime() {
    if [[ "${1-}" != "run" ]]; then
        _project_secrets_fail runtime-operation "${1-}"
        return 1
    fi
    if [[ "$PROJECT_SECRETS_PAIR_STATE" != "present" ]]; then
        _project_secrets_fail staging-unavailable ""
        return 1
    fi
    if [[ -z "${PROJECT_SECRET_NAMES-}" ]]; then
        _project_secrets_fail metadata-not-validated ""
        return 1
    fi
    if [[ -z "${PROJECT_SECRETS_STAGING_DIR-}" ]]; then
        _project_secrets_fail staging-unavailable ""
        return 1
    fi
    if [[ ":$SHELLOPTS:" != *":posix:"* ]]; then
        _project_secrets_fail runtime-context ""
        return 1
    fi
    _PROJECT_SECRETS_EXEC_ARGS=( "$@" )
    # Compose with a launcher-owned EXIT trap instead of replacing it:
    # capture the currently installed action (empty when there is none)
    # for the exit handler to run after the library cleanup. The capture
    # never chains the library's own handler onto itself, so a second
    # isolated invocation keeps the launcher's original trap.
    _PROJECT_SECRETS_EXIT_CURRENT="$(trap -p EXIT)"
    case "$_PROJECT_SECRETS_EXIT_CURRENT" in
        "trap -- '_project_secrets_exit_handler' EXIT" \
            | "trap -- _project_secrets_exit_handler EXIT") ;;
        *)
            _PROJECT_SECRETS_EXIT_CURRENT="${_PROJECT_SECRETS_EXIT_CURRENT#trap -- }"
            _PROJECT_SECRETS_PRIOR_EXIT="${_PROJECT_SECRETS_EXIT_CURRENT% EXIT}"
            ;;
    esac
    unset _PROJECT_SECRETS_EXIT_CURRENT
    trap '_project_secrets_exit_handler' EXIT
    _PROJECT_SECRETS_EXEC_STATUS=0
    (
        _PROJECT_SECRETS_CHILD=1
        _project_secrets_sanitize_shell
        if ! _project_secrets_build_runtime_argv "${_PROJECT_SECRETS_EXEC_ARGS[@]}"; then
            exit 125
        fi
        project_secrets_prepare_runtime || exit 125
        exec "$PROJECT_SECRETS_MSB_PATH" "${_PROJECT_SECRETS_RUNTIME_ARGV[@]}"
    ) || _PROJECT_SECRETS_EXEC_STATUS=$?
    _project_secrets_exit_cleanup
    return "$_PROJECT_SECRETS_EXEC_STATUS"
}
