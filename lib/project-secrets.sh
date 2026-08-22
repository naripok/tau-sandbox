#!/bin/bash
# lib/project-secrets.sh — pass-through project-secret discovery.
#
# Maps a launch directory to the user's hidden per-project secret
# directory, sanity-checks the paired sources found there, and exports
# their paths so the launcher can source the values and hand the policy
# to the runtime unmodified via `msb run --secret-conf`. Content
# validation (policy grammar, value resolution, placeholder injection)
# is the runtime's documented --secret-conf contract, not this
# library's. Single-user trusted host: sourced files are trusted
# configuration. The library never writes to stdout and never prints
# secret content; failures are one stderr line naming a path or
# variable.
#
# Bash 4.0 compatible.

# Public state, set by project_secrets_prepare. Paths and names are
# empty (never unset) when PROJECT_SECRETS_PAIR_STATE is "none".
PROJECT_SECRETS_PAIR_STATE="none"
PROJECT_SECRETS_POLICY_PATH=""
PROJECT_SECRETS_ENV_PATH=""
PROJECT_SECRETS_NAMES=""

# Reserved guest names: shell- and runtime-critical variables plus the
# BASH and TAU_ prefixes (entrypoint internals live under TAU_ENTRYPOINT_).
_PROJECT_SECRETS_RESERVED_RE='^(HOME|SHELL|TERM|COLORTERM|USER|LOGNAME|PATH|IFS|PWD|OLDPWD|SHLVL|BASH_ENV|ENV|LD_PRELOAD|LD_LIBRARY_PATH|PYTHONHOME|PYTHONPATH|NODE_OPTIONS)$|^(BASH|TAU_)'

_project_secrets_fail() {
    printf 'project-secrets: %s\n' "$1" >&2
    return 1
}

# project_secrets_lexical_path(path, base) — print the absolute lexical
# form of path, resolving a relative path against base and removing "."
# and ".." segments with parameter expansion. Symlinks are never
# followed; base must be absolute.
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
    remaining="${combined#/}"
    while [[ -n "$remaining" ]]; do
        component="${remaining%%/*}"
        if [[ "$remaining" == */* ]]; then
            remaining="${remaining#*/}"
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

# project_secrets_physical_path(path) — print the canonical absolute
# form of an existing entry. Directory components resolve through
# cd -P/pwd -P; a non-directory final entry keeps its resolved parent
# and exact basename. Returns nonzero when resolution fails.
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

# project_secrets_prepare(launch_dir, home, projects_mode, projects_value)
# — one-call discovery and sanity checks. projects_mode is "default"
# (${home}/Projects; discovery disables when it is not a usable
# directory) or "explicit" (projects_value is TAU_PROJECTS_DIR, resolved
# from the launch directory when relative; unusable values fail).
# Physical project membership is authoritative; the derived directory
# mirrors the physical relative path under home with a dot-prefixed
# first component. Exports PROJECT_SECRETS_PAIR_STATE and, for a
# present pair, PROJECT_SECRETS_POLICY_PATH, PROJECT_SECRETS_ENV_PATH,
# and PROJECT_SECRETS_NAMES (deduplicated declared names).
project_secrets_prepare() {
    local launch_dir="$1" home="$2" mode="$3" value="$4"
    local launch_lex home_phys root_lex root_phys launch_phys rel dir dir_phys
    local env_path policy_path names name

    PROJECT_SECRETS_PAIR_STATE="none"
    PROJECT_SECRETS_POLICY_PATH=""
    PROJECT_SECRETS_ENV_PATH=""
    PROJECT_SECRETS_NAMES=""

    case "$mode" in
        default | explicit) ;;
        *) _project_secrets_fail "invalid projects mode: $mode" || return ;;
    esac
    if [[ "$home" != /* || ! -d "$home" ]]; then
        _project_secrets_fail "invalid home directory: $home" || return
    fi
    if [[ "$launch_dir" != /* || ! -d "$launch_dir" ]]; then
        _project_secrets_fail "invalid launch directory: $launch_dir" || return
    fi
    launch_lex="$(project_secrets_lexical_path "$launch_dir" "$PWD")"
    launch_phys="$(project_secrets_physical_path "$launch_lex")" || {
        _project_secrets_fail "invalid launch directory: $launch_lex" || return
    }
    # The physical home anchors both the default root and the derived
    # directory so containment comparisons stay in one path space even
    # when a home component is itself a symlink (e.g. /tmp on macOS).
    home_phys="$(project_secrets_physical_path "$(project_secrets_lexical_path "$home" "$PWD")")" || {
        _project_secrets_fail "invalid home directory: $home" || return
    }

    if [[ "$mode" == "default" ]]; then
        root_lex="$home_phys/Projects"
        if [[ ! -d "$root_lex" || ! -r "$root_lex" || ! -x "$root_lex" ]]; then
            return 0
        fi
    else
        if [[ -z "$value" ]]; then
            _project_secrets_fail "invalid TAU_PROJECTS_DIR: empty value" || return
        fi
        root_lex="$(project_secrets_lexical_path "$value" "$launch_lex")"
        if [[ ! -d "$root_lex" || ! -r "$root_lex" || ! -x "$root_lex" ]]; then
            _project_secrets_fail "invalid TAU_PROJECTS_DIR: $value" || return
        fi
    fi
    root_phys="$(project_secrets_physical_path "$root_lex")" || {
        _project_secrets_fail "invalid projects root: $root_lex" || return
    }

    # Physical membership is authoritative; root and outside launches
    # derive no secrets.
    if [[ "$launch_phys" != "$root_phys"/* ]]; then
        return 0
    fi
    rel="${launch_phys#"$root_phys"/}"
    dir="$home_phys/.${rel}"

    if [[ ! -e "$dir" && ! -L "$dir" ]]; then
        return 0
    fi
    if [[ ! -d "$dir" || ! -r "$dir" || ! -x "$dir" ]]; then
        _project_secrets_fail "invalid secret directory: $dir" || return
    fi
    dir_phys="$(project_secrets_physical_path "$dir")" || {
        _project_secrets_fail "invalid secret directory: $dir" || return
    }
    # The secret directory must stay outside the projects root: a
    # symlink escape would put secret sources on mounted project data.
    if [[ "$dir_phys" == "$root_phys" || "$dir_phys" == "$root_phys"/* ]]; then
        _project_secrets_fail "secret directory escapes into projects root: $dir" || return
    fi

    env_path="$dir/secrets.env"
    policy_path="$dir/secrets.yaml"
    if [[ ! -e "$env_path" && ! -L "$env_path" \
        && ! -e "$policy_path" && ! -L "$policy_path" ]]; then
        return 0
    fi
    if [[ ! -e "$policy_path" && ! -L "$policy_path" ]]; then
        _project_secrets_fail "missing secret source: $policy_path" || return
    fi
    if [[ ! -e "$env_path" && ! -L "$env_path" ]]; then
        _project_secrets_fail "missing secret source: $env_path" || return
    fi
    if [[ ! -f "$env_path" || ! -r "$env_path" ]]; then
        _project_secrets_fail "invalid secret source: $env_path" || return
    fi
    if [[ ! -f "$policy_path" || ! -r "$policy_path" ]]; then
        _project_secrets_fail "invalid secret source: $policy_path" || return
    fi

    # Declared names: top-level NAME= / export NAME= assignments only.
    names="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' \
        "$env_path" | sort -u)" || {
        _project_secrets_fail "cannot read secret source: $env_path" || return
    }
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        if [[ "$name" =~ $_PROJECT_SECRETS_RESERVED_RE ]]; then
            _project_secrets_fail "reserved secret name: $name" || return
        fi
        PROJECT_SECRETS_NAMES+="${PROJECT_SECRETS_NAMES:+ }$name"
    done <<< "$names"

    PROJECT_SECRETS_PAIR_STATE="present"
    PROJECT_SECRETS_ENV_PATH="$dir_phys/secrets.env"
    PROJECT_SECRETS_POLICY_PATH="$dir_phys/secrets.yaml"
    return 0
}
