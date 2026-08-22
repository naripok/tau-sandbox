#!/bin/bash
set -euo pipefail

# Launch the Tau agent sandbox: a hardware-isolated microsandbox microVM.
# The VM is ephemeral; durable home, session, and log state lives in isolated
# per-project named volumes.

# --- Configuration (host side) ---
# TAU_IMAGE: full image reference passed to msb (bypasses .tau-packages).
# TAU_CONFIG_DIR: host resources exposed read-only for startup synchronization
# sources, except for the writable credentials file. TAU_AGENTS_DIR remains
# read-only at its normal sandbox-home path.
# TAU_ENV_FILE: env file whose variables are forwarded into the VM.
IMAGE_NAME="${TAU_IMAGE:-tau-agent-isolated}"
# Project-local config discovery: when TAU_CONFIG_DIR is unset, the nearest
# ancestor of the launch directory whose `.tau` entry is a directory (a real
# per-project config or a symlink to one) supplies the config directory. This
# mirrors the .tau-packages project-local convention. TAU_CONFIG_DIR always
# wins; without a match the default (~/.tau) applies.
CONFIG_DIR="${TAU_CONFIG_DIR:-}"
if [ -z "$CONFIG_DIR" ]; then
    probe_dir="$(pwd)"
    while :; do
        if [ -d "$probe_dir/.tau" ]; then
            CONFIG_DIR="$probe_dir/.tau"
            break
        fi
        parent="$(dirname "$probe_dir")"
        [ "$parent" = "$probe_dir" ] && break
        probe_dir="$parent"
    done
fi
CONFIG_DIR="${CONFIG_DIR:-${HOME}/.tau}"
AGENTS_DIR="${TAU_AGENTS_DIR:-${HOME}/.agents}"
ENV_FILE="${TAU_ENV_FILE:-${HOME}/.env}"
# Resolve a caller-supplied relative TAU_ENV_FILE against the launch
# directory before any use: slash-less relative names must resolve from
# the launch directory, not from PATH or the eventual working directory.
case "$ENV_FILE" in
    /*) ;;
    *) ENV_FILE="$(pwd)/$ENV_FILE" ;;
esac
CPUS="${TAU_CPUS:-4}"
MEM="${TAU_MEM:-8G}"
PIDS="${TAU_PIDS:-1024}"
# TAU_LAN_HOSTS: comma-separated exact-IP egress exceptions to the public
# network profile; empty (default) keeps every private address denied.
LAN_HOSTS="${TAU_LAN_HOSTS:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Raw lexical exposure sources, captured before the canonicalization below:
# the project-secrets library resolves physical identities itself, and the
# captured bootstrap entry list must reflect exactly the entries the launch
# will snapshot.
RAW_CONFIG_DIR="$CONFIG_DIR"
RAW_AGENTS_DIR="$AGENTS_DIR"
[ -d "$CONFIG_DIR" ] && CONFIG_DIR="$(realpath "$CONFIG_DIR")"
[ -d "$AGENTS_DIR" ] && AGENTS_DIR="$(realpath "$AGENTS_DIR")"

# Derive persistent volume names from the project path.
# The basename makes "msb volume ls" output meaningful.
# The 8-char hash suffix guarantees uniqueness.
PROJECT_PATH="$(realpath "$(pwd)")"
PROJECT_NAME="$(basename "$PROJECT_PATH")"
PROJECT_HASH="$(echo "$PROJECT_PATH" | sha256sum | cut -c1-8)"

sanitize_project_name() {
    # Map an arbitrary basename to a safe name: lowercase, every run of
    # characters outside [a-z0-9] collapsed to a single underscore, leading
    # and trailing underscores removed, truncated so derived names fit a
    # 255-byte path component and a 255-char OCI reference. Uniqueness is
    # carried by the path hash, not by the name.
    local out
    out="$(printf '%s' "$1" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C tr -cs 'a-z0-9' '_')"
    out="${out#_}"
    out="${out%_}"
    out="${out:0:218}"
    out="${out%_}"
    [ -n "$out" ] || out="project"
    printf '%s' "$out"
}

# Volume names must be legal msb volume names: [A-Za-z0-9._-] (the tau-*
# prefix supplies the required alphanumeric start) and short enough for a
# 255-byte path component in the microsandbox volume store (longest prefix
# "tau-sessions-" plus the 8-char hash leaves 233). Keep the raw basename
# when legal so existing per-project volumes stay put; sanitize otherwise —
# such projects could never have launched.
VOLUME_NAME_RE='^[A-Za-z0-9._-]{1,233}$'
if [[ ! "$PROJECT_NAME" =~ $VOLUME_NAME_RE ]]; then
    PROJECT_NAME="$(sanitize_project_name "$PROJECT_NAME")"
fi

# Image names must be legal OCI reference path components: lowercase
# [a-z0-9._-] with no adjacent separators (e.g. the "-." produced by a dot
# directory), at most 255 chars for the full reference name. Keep the raw
# basename when the derived name is legal so existing per-project images
# stay put; sanitize otherwise.
IMAGE_PROJECT_NAME="$PROJECT_NAME"
IMAGE_NAME_PROBE="tau-agent-isolated-${IMAGE_PROJECT_NAME}-00000000-00000000"
IMAGE_NAME_RE='^[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*$'
if [[ ! "$IMAGE_NAME_PROBE" =~ $IMAGE_NAME_RE ]] || [ "${#IMAGE_NAME_PROBE}" -gt 255 ]; then
    IMAGE_PROJECT_NAME="$(sanitize_project_name "$PROJECT_NAME")"
fi

PERSIST_VOLUME="tau-persist-${PROJECT_NAME}-${PROJECT_HASH}"
SESSIONS_VOLUME="tau-sessions-${PROJECT_NAME}-${PROJECT_HASH}"
LOGS_VOLUME="tau-logs-${PROJECT_NAME}-${PROJECT_HASH}"

# Handle --reset flag: remove all per-project volumes and exit. This runs
# before any project-secret discovery, so a reset performs no secret work
# even with invalid sources.
if [ "${1:-}" = "--reset" ]; then
    msb volume rm "$PERSIST_VOLUME" "$SESSIONS_VOLUME" "$LOGS_VOLUME" >/dev/null 2>&1 || true
    echo "Volumes $PERSIST_VOLUME, $SESSIONS_VOLUME, and $LOGS_VOLUME removed."
    exit 0
fi

# --- Project secrets (exact-directory pair) ---
# The pass-through library owns discovery and sanity checks; it is sourced
# here but never called before the --reset handler above.
# shellcheck source=lib/project-secrets.sh
source "$SCRIPT_DIR/lib/project-secrets.sh"

BOOTSTRAP_STAGE=""
# One idempotent exit cleanup, registered for every exit path: remove the
# bootstrap snapshot this launcher created.
cleanup() {
    if [ -n "$BOOTSTRAP_STAGE" ]; then
        rm -rf -- "$BOOTSTRAP_STAGE"
        BOOTSTRAP_STAGE=""
    fi
    return 0
}
trap cleanup EXIT


MSB_BIN="msb"

# --- Per-project package handling ---
# Must be AFTER the --reset handler so `run.sh --reset` works even with an
# invalid .tau-packages file.

parse_packages() {
    # Parse .tau-packages: strip whitespace and CRLF, skip comments/blanks.
    # Output: space-separated package list on stdout.
    local file="$1"
    if [ ! -f "$file" ]; then
        echo ""
        return
    fi
    sed 's/\r$//' "$file" | \
        sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | \
        { grep -v '^#' || true; } | \
        { grep -v '^$' || true; } | \
        tr '\n' ' ' || true
}

validate_packages() {
    # Reject .tau-packages lines containing shell metacharacters.
    # Returns 1 and prints an error if dangerous characters are found.
    local file="$1"
    if [ ! -f "$file" ]; then
        return 0
    fi
    local invalid_line
    invalid_line=$(sed 's/\r$//' "$file" | \
        sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | \
        { grep -v '^#' || true; } | \
        { grep -v '^$' || true; } | \
        grep -n '[;|$\`&><*?~\\!]' || true)
    if [ -n "$invalid_line" ]; then
        echo "Error: .tau-packages contains dangerous characters:" >&2
        echo "$invalid_line" >&2
        echo "Only alphanumeric characters, hyphens, dots, and underscores are allowed." >&2
        return 1
    fi
    return 0
}

compute_hash() {
    # Deterministic hash of .tau-packages raw bytes (first 8 hex chars).
    local file="$1"
    if [ ! -f "$file" ]; then
        echo ""
        return
    fi
    sha256sum "$file" | cut -c1-8
}

compute_base_hash() {
    # Freshness key for the base the package image is baked from: the
    # Containerfile and every regular file under config/ (dotfiles included),
    # digests concatenated in bytewise path order. Directories and other
    # non-regular entries are skipped. A missing Containerfile or config/
    # directory aborts loudly: a package launch must never derive a tag whose
    # freshness cannot be verified against the current inputs.
    [ -f "$SCRIPT_DIR/Containerfile" ] || {
        echo "Error: $SCRIPT_DIR/Containerfile is missing; cannot derive a package image tag." >&2
        exit 1
    }
    [ -d "$SCRIPT_DIR/config" ] || {
        echo "Error: $SCRIPT_DIR/config is missing; cannot derive a package image tag." >&2
        exit 1
    }
    {
        sha256sum "$SCRIPT_DIR/Containerfile"
        find "$SCRIPT_DIR/config" -maxdepth 1 -type f -print0 |
            LC_ALL=C sort -z | xargs -0 -r sha256sum
    } | awk '{ printf "%s", $1 }' | sha256sum | cut -c1-8
}

prune_superseded_package_images() {
    # Cache hygiene after a package-image build: remove the images this build
    # supersedes — the legacy single-hash tag of the current package content
    # and any older base version of it. Images tagged with any other package
    # hash (same-image-name projects, earlier .tau-packages contents) are
    # never removed. Failed removals are tolerated: pruning never blocks the
    # launch.
    local hex8='[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    local cached_ref
    while IFS= read -r cached_ref; do
        case "$cached_ref" in
            "localhost/tau-agent-isolated-${IMAGE_PROJECT_NAME}-${PKG_HASH}:latest" | \
            "localhost/tau-agent-isolated-${IMAGE_PROJECT_NAME}-"${hex8}"-${PKG_HASH}:latest")
                [ "$cached_ref" = "$IMAGE_REF" ] && continue
                "$MSB_BIN" rmi "$cached_ref" >/dev/null 2>&1 || true
                ;;
        esac
    done < <("$MSB_BIN" images -q)
}

# Image reference resolution.
# TAU_IMAGE overrides everything: the reference is passed to msb verbatim
# and image management (build/load) is skipped entirely — the user manages
# that image externally (e.g. `make build`). Otherwise, use per-project
# naming when .tau-packages lists packages, else the shared base image.
# Per-project names embed a hash of the base inputs (Containerfile and
# config/), so base updates invalidate the tag and trigger the approval-
# gated rebuild on the next run.
if [ -n "${TAU_IMAGE:-}" ]; then
    IMAGE_REF="$TAU_IMAGE"
    HAS_PACKAGES=0
    SKIP_IMAGE_CHECK=1
else
    EXTRA_PACKAGES=""
    HAS_PACKAGES=0
    if [ -f ".tau-packages" ]; then
        validate_packages ".tau-packages" || exit 1
        EXTRA_PACKAGES=$(parse_packages ".tau-packages")
        if [ -n "$(echo "$EXTRA_PACKAGES" | tr -d '[:space:]')" ]; then
            HAS_PACKAGES=1
        fi
    fi
    if [ "$HAS_PACKAGES" -eq 1 ]; then
        PKG_HASH=$(compute_hash ".tau-packages")
        BASE_HASH=$(compute_base_hash)
        IMAGE_NAME="tau-agent-isolated-${IMAGE_PROJECT_NAME}-${BASE_HASH}-${PKG_HASH}"
    fi
    IMAGE_REF="localhost/${IMAGE_NAME}:latest"
fi

# Build and load the image into the microsandbox cache if it is missing.
if [ "${SKIP_IMAGE_CHECK:-0}" != "1" ] && ! "$MSB_BIN" images -q | grep -qx "$IMAGE_REF"; then
    if [ "$HAS_PACKAGES" -eq 1 ] && [ -t 0 ]; then
        echo ""
        echo "[!] Building sandbox image with extra packages:"
        for pkg in $EXTRA_PACKAGES; do
            echo "       $pkg"
        done
        echo ""
        read -r -p "Approve? [y/N] " APPROVAL
        if [ "$APPROVAL" != "y" ] && [ "$APPROVAL" != "Y" ]; then
            echo "Aborted. Extra packages not installed." >&2
            exit 1
        fi
    elif [ "$HAS_PACKAGES" -eq 1 ]; then
        echo "" >&2
        echo "Error: .tau-packages requires an image rebuild but stdin is not a terminal." >&2
        echo "Run interactively or set TAU_IMAGE to bypass." >&2
        exit 1
    fi

    echo "Building image ${IMAGE_REF}..."
    if [ "$HAS_PACKAGES" -eq 1 ]; then
        podman build \
            --build-arg EXTRA_PACKAGES="$EXTRA_PACKAGES" \
            -t "$IMAGE_NAME" "$SCRIPT_DIR"
    else
        podman build -t "$IMAGE_NAME" "$SCRIPT_DIR"
    fi
    podman save "$IMAGE_NAME" | "$MSB_BIN" load
    if [ "$HAS_PACKAGES" -eq 1 ]; then
        prune_superseded_package_images
    fi
fi

# --- Environment forwarding ---
# Forward variables defined in the env file, mirroring pi-sandbox.
# Values pass through as arguments to msb; they are never echoed by this
# script and never baked into the image.
ENV_ARGS=()
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

# --- Project secrets ---
# Discover the exact-directory pair after the trusted env file so sourced
# secret values win over same-named ordinary assignments in the runtime's
# inherited environment. TAU_PROJECTS_DIR: unset uses the default
# ${HOME}/Projects; an explicitly empty value is invalid; relative values
# resolve from the launch directory.
if [ -n "${TAU_PROJECTS_DIR+x}" ]; then
    PROJECTS_MODE="explicit"
    PROJECTS_VALUE="$TAU_PROJECTS_DIR"
else
    PROJECTS_MODE="default"
    PROJECTS_VALUE=""
fi
project_secrets_prepare "$(pwd)" "$HOME" "$PROJECTS_MODE" "$PROJECTS_VALUE" || exit 1
if [ "$PROJECT_SECRETS_PAIR_STATE" = "present" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$PROJECT_SECRETS_ENV_PATH"
    set +a
fi

if [ -f "$ENV_FILE" ]; then
    while IFS= read -r key; do
        [[ -z "$key" ]] && continue
        # A name declared as a project secret is suppressed from raw
        # forwarding; the runtime placeholder supplies the guest variable.
        case " $PROJECT_SECRETS_NAMES " in
            *" $key "*) continue ;;
        esac
        ENV_ARGS+=(-e "${key}=${!key:-}")
    done < <(awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        {
            gsub(/^[[:space:]]*export[[:space:]]+/, "")
            match($0, /^[[:space:]]*[^=[:space:]]+/)
            if (RLENGTH > 0) {
                key = substr($0, RSTART, RLENGTH)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
                print key
            }
        }
    ' "$ENV_FILE")
fi

# --- Mounts ---
# The project and per-project home are writable. Existing top-level entries in
# host ~/.tau are copied into a temporary snapshot with symlinks recursively
# dereferenced, then mounted individually read-only under the bootstrap
# directory. The entrypoint refreshes them in the persistent home on every
# start, where Tau can use
# atomic replacement without modifying the host defaults. credentials.json is
# the sole host write exception. Sessions, logs, and trust state stay
# per-project; the Tau wrapper makes OAuth writes safe for a mounted credential
# file. Session, log, and credential mounts use backing paths outside the home
# so microsandbox's root-owned mountpoint setup cannot create an unwritable
# ~/.tau before the unprivileged entrypoint runs. Host history and trust
# decisions are never exposed or modified.
# host-perms=mirror: mirror guest rwx bits to the host inode so sandbox-created
# files (including +x scripts) keep their modes on the host and git stays clean.
MOUNT_ARGS=(
    -v "$(pwd):/workspace:host-perms=mirror"
    -v "$PERSIST_VOLUME:/home/tau"
)

# Top-level host config entries are copied with links recursively
# dereferenced into a private temporary snapshot and mounted individually
# read-only under the bootstrap directory.
if [ -d "$RAW_CONFIG_DIR" ]; then
    shopt -s dotglob nullglob
    BOOTSTRAP_ENTRIES=( "$RAW_CONFIG_DIR"/* )
    shopt -u dotglob nullglob
else
    BOOTSTRAP_ENTRIES=()
fi
for entry in "${BOOTSTRAP_ENTRIES[@]}"; do
    name="${entry##*/}"
    case "$name" in
        credentials.json|sessions|logs|trust.json|trust.json.lock|trust.json.pending|.host-config-bootstrapped|.host-config-synced)
            continue
            ;;
    esac
    resolved_entry="$(realpath -e "$entry" 2>/dev/null || true)"
    if [ ! -f "$resolved_entry" ] && [ ! -d "$resolved_entry" ]; then
        continue
    fi
    if [ -z "$BOOTSTRAP_STAGE" ]; then
        BOOTSTRAP_STAGE="$(mktemp -d "${TMPDIR:-/tmp}/tau-sandbox-bootstrap.XXXXXX")"
    fi
    # Dereference both top-level and nested links while still on the host,
    # where absolute host paths are meaningful. The VM receives only the
    # resulting snapshot, never broken links back into the host filesystem.
    if ! cp -aL -- "$entry" "$BOOTSTRAP_STAGE/$name"; then
        echo "Error: failed to snapshot host Tau config entry: $entry" >&2
        exit 1
    fi
    MOUNT_ARGS+=(-v "$BOOTSTRAP_STAGE/$name:/etc/tau-sandbox/bootstrap/tau/$name:ro")
done
if [ -f "$CONFIG_DIR/credentials.json" ] && [ ! -L "$CONFIG_DIR/credentials.json" ]; then
    MOUNT_ARGS+=(-v "$CONFIG_DIR/credentials.json:/etc/tau-sandbox/shared/credentials.json")
    ENV_ARGS+=(-e "TAU_SANDBOX_SHARED_CREDENTIALS=1")
else
    ENV_ARGS+=(-e "TAU_SANDBOX_SHARED_CREDENTIALS=0")
fi
[ -d "$AGENTS_DIR" ] && MOUNT_ARGS+=(-v "$AGENTS_DIR:/home/tau/.agents:ro")
MOUNT_ARGS+=(
    -v "$SESSIONS_VOLUME:/var/lib/tau-sandbox/sessions"
    -v "$LOGS_VOLUME:/var/lib/tau-sandbox/logs"
    -v "$SCRIPT_DIR/config/APPEND_SYSTEM.md:/etc/tau-sandbox/APPEND_SYSTEM.md:ro"
)

# --- Run ---
# The public profile allows internet egress and gateway DNS. TAU_LAN_HOSTS
# adds one narrow exact-IP rule per entry; all other private addresses remain
# denied. Inbound stays closed because no ports are published. The low-level
# --net-default-ingress deny path is intentionally avoided because it silently
# dropped microsandbox's DNS allow rule.
NET_RULES=()
if [ -n "$LAN_HOSTS" ]; then
    if ! printf '%s' "$LAN_HOSTS" | grep -qE '^[0-9A-Za-z.:-]+(,[0-9A-Za-z.:-]+)*$'; then
        echo "Error: TAU_LAN_HOSTS contains invalid entries." >&2
        echo "Expected comma-separated IP addresses or hostnames." >&2
        exit 1
    fi
    IFS=',' read -r -a LAN_HOST_ARRAY <<< "$LAN_HOSTS"
    for lan_host in "${LAN_HOST_ARRAY[@]}"; do
        NET_RULES+=(--net-rule "allow@${lan_host}")
    done
fi

RUN_ARGS=(
    "${MOUNT_ARGS[@]}"
    -c "$CPUS"
    -m "$MEM"
    --rlimit "nproc=${PIDS}"
    --security restricted
    --tmpfs /tmp
    --user 1000:1000
    --net public
    "${NET_RULES[@]}"
    --label "project=${PROJECT_NAME}"
    -w /workspace
    "${ENV_ARGS[@]}"
    "$IMAGE_REF"
    -- "$@"
)
if [ "$PROJECT_SECRETS_PAIR_STATE" = "present" ]; then
    # Exactly one --secret-conf naming the pair's own policy, immediately
    # after `run`; the guest argv after `--` is preserved byte-for-byte.
    "$MSB_BIN" run --secret-conf "$PROJECT_SECRETS_POLICY_PATH" "${RUN_ARGS[@]}"
else
    "$MSB_BIN" run "${RUN_ARGS[@]}"
fi
