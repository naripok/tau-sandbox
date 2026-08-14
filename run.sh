#!/bin/bash
set -euo pipefail

# Launch the Tau agent sandbox: a hardware-isolated microsandbox microVM.
# The VM is ephemeral (discarded after every run); durable state lives in a
# per-project persistent named volume mounted at /home/tau.

# --- Configuration (host side) ---
# TAU_IMAGE: full image reference passed to msb (bypasses .tau-packages).
# TAU_CONFIG_DIR/TAU_AGENTS_DIR: host config mounted into the sandbox home
# so the agent reads and writes the same config as the host.
# TAU_ENV_FILE: env file whose variables are forwarded into the VM.
IMAGE_NAME="${TAU_IMAGE:-tau-agent-isolated}"
CONFIG_DIR="${TAU_CONFIG_DIR:-${HOME}/.tau}"
AGENTS_DIR="${TAU_AGENTS_DIR:-${HOME}/.agents}"
ENV_FILE="${TAU_ENV_FILE:-${HOME}/.env}"
CPUS="${TAU_CPUS:-4}"
MEM="${TAU_MEM:-8G}"
PIDS="${TAU_PIDS:-1024}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Derive persistent volume name from project path.
# The basename makes "msb volume ls" output meaningful.
# The 8-char hash suffix guarantees uniqueness.
PROJECT_PATH="$(realpath "$(pwd)")"
PROJECT_NAME="$(basename "$PROJECT_PATH")"
PERSIST_VOLUME="tau-persist-${PROJECT_NAME}-$(echo "$PROJECT_PATH" | sha256sum | cut -c1-8)"

# Handle --reset flag: remove the persistent volume and exit.
if [ "${1:-}" = "--reset" ]; then
    msb volume rm "$PERSIST_VOLUME" >/dev/null 2>&1 || true
    echo "Volume $PERSIST_VOLUME removed."
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
# Workspace + persistent home are always mounted. The host's Tau config
# dirs are mounted read-write into the sandbox home (nested inside the
# volume mount) so the agent picks up the host's login tokens, skills, and
# sessions; writes reach the host via identity virtualization. Mounts are
# added only when the source directory exists.
MOUNT_ARGS=(
    -v "$(pwd):/workspace"
    -v "$PERSIST_VOLUME:/home/tau"
)
[ -d "$CONFIG_DIR" ] && MOUNT_ARGS+=(-v "$CONFIG_DIR:/home/tau/.tau")
[ -d "$AGENTS_DIR" ] && MOUNT_ARGS+=(-v "$AGENTS_DIR:/home/tau/.agents")

# --- Run ---
# Public network profile: msb auto-allows egress and gateway DNS, and keeps
# inbound closed (only published ports accept traffic; none are published
# here). The low-level --net-default-ingress deny path silently dropped the
# DNS allow rule, so every hostname lookup failed with EAI_NONAME.
exec msb run \
    "${MOUNT_ARGS[@]}" \
    -c "$CPUS" \
    -m "$MEM" \
    --rlimit "nproc=${PIDS}" \
    --security restricted \
    --user 1000:1000 \
    --net public \
    --label "project=${PROJECT_NAME}" \
    -w /workspace \
    "${ENV_ARGS[@]}" \
    "$IMAGE_REF" \
    -- "$@"
