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
CONFIG_DIR="${TAU_CONFIG_DIR:-${HOME}/.tau}"
AGENTS_DIR="${TAU_AGENTS_DIR:-${HOME}/.agents}"
ENV_FILE="${TAU_ENV_FILE:-${HOME}/.env}"
CPUS="${TAU_CPUS:-4}"
MEM="${TAU_MEM:-8G}"
PIDS="${TAU_PIDS:-1024}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -d "$CONFIG_DIR" ] && CONFIG_DIR="$(realpath "$CONFIG_DIR")"
[ -d "$AGENTS_DIR" ] && AGENTS_DIR="$(realpath "$AGENTS_DIR")"

# Derive persistent volume name from project path.
# The basename makes "msb volume ls" output meaningful.
# The 8-char hash suffix guarantees uniqueness.
PROJECT_PATH="$(realpath "$(pwd)")"
PROJECT_NAME="$(basename "$PROJECT_PATH")"
PROJECT_HASH="$(echo "$PROJECT_PATH" | sha256sum | cut -c1-8)"
PERSIST_VOLUME="tau-persist-${PROJECT_NAME}-${PROJECT_HASH}"
SESSIONS_VOLUME="tau-sessions-${PROJECT_NAME}-${PROJECT_HASH}"
LOGS_VOLUME="tau-logs-${PROJECT_NAME}-${PROJECT_HASH}"

# Handle --reset flag: remove all per-project volumes and exit.
if [ "${1:-}" = "--reset" ]; then
    msb volume rm "$PERSIST_VOLUME" "$SESSIONS_VOLUME" "$LOGS_VOLUME" >/dev/null 2>&1 || true
    echo "Volumes $PERSIST_VOLUME, $SESSIONS_VOLUME, and $LOGS_VOLUME removed."
    exit 0
fi

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

# Image reference resolution.
# TAU_IMAGE overrides everything: the reference is passed to msb verbatim
# and image management (build/load) is skipped entirely — the user manages
# that image externally (e.g. `make build`). Otherwise, use per-project
# naming when .tau-packages lists packages, else the shared base image.
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
        IMAGE_NAME="tau-agent-isolated-${PROJECT_NAME}-${PKG_HASH}"
    fi
    IMAGE_REF="localhost/${IMAGE_NAME}:latest"
fi

# Build and load the image into the microsandbox cache if it is missing.
if [ "${SKIP_IMAGE_CHECK:-0}" != "1" ] && ! msb images -q | grep -qx "$IMAGE_REF"; then
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
    podman save "$IMAGE_NAME" | msb load
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

    while IFS= read -r key; do
        [[ -z "$key" ]] && continue
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
MOUNT_ARGS=(
    -v "$(pwd):/workspace"
    -v "$PERSIST_VOLUME:/home/tau"
)
BOOTSTRAP_STAGE=""
cleanup_bootstrap_stage() {
    if [ -n "$BOOTSTRAP_STAGE" ]; then
        rm -rf -- "$BOOTSTRAP_STAGE"
    fi
}
trap cleanup_bootstrap_stage EXIT

if [ -d "$CONFIG_DIR" ]; then
    shopt -s dotglob nullglob
    for entry in "$CONFIG_DIR"/*; do
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
    shopt -u dotglob nullglob
fi
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
# The public profile allows internet egress and gateway DNS. Add one narrow
# exception for the LAN GPU server; all other private addresses remain denied.
# Inbound stays closed because no ports are published. The low-level
# --net-default-ingress deny path is intentionally avoided because it silently
# dropped microsandbox's DNS allow rule.
msb run \
    "${MOUNT_ARGS[@]}" \
    -c "$CPUS" \
    -m "$MEM" \
    --rlimit "nproc=${PIDS}" \
    --security restricted \
    --tmpfs /tmp \
    --user 1000:1000 \
    --net public \
    --net-rule "allow@192.168.15.9" \
    --label "project=${PROJECT_NAME}" \
    -w /workspace \
    "${ENV_ARGS[@]}" \
    "$IMAGE_REF" \
    -- "$@"
