#!/bin/bash
set -euo pipefail

# Install the tau-agent sandbox.
# Checks prerequisites, builds the image and loads it into the microsandbox
# cache, then prints the alias to add to your shellrc.

IMAGE_NAME="${TAU_IMAGE:-tau-agent-isolated}"
IMAGE_REF="localhost/${IMAGE_NAME}:latest"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
fail()  { echo -e "${RED}[x]${NC} $*"; exit 1; }

# --- Prerequisites ---

# Bash 4+ (needed for arrays)
bash_version="${BASH_VERSINFO[0]}"
[ "$bash_version" -ge 4 ] || fail "Bash 4+ required (have $bash_version)"

# microsandbox CLI
command -v msb >/dev/null 2>&1 || fail "msb not found. Install it first: https://docs.microsandbox.dev/quickstart"
info "msb is working ($(msb --version 2>/dev/null | head -1))"

# podman (used to build the OCI image that msb runs)
command -v podman >/dev/null 2>&1 || fail "podman not found. Install it first: https://podman.io/docs/installation"

# KVM is required on Linux (hardware virtualization). macOS uses
# Virtualization.framework and needs no /dev/kvm.
if [ "$(uname -s)" = "Linux" ] && [ ! -e /dev/kvm ]; then
    fail "KVM not available (/dev/kvm missing). On Linux, microsandbox needs a kernel with KVM enabled (e.g. modprobe kvm_amd / kvm_intel)."
fi

# msb doctor: non-fatal report of host prerequisites/performance
msb doctor >/dev/null 2>&1 || warn "msb doctor reported host issues; solve them if sandboxes fail to boot."

# --- Build + load image ---
# TAU_IMAGE bypasses automatic image management; the reference must be
# loaded by the user (e.g. `make build`).
if [ -n "${TAU_IMAGE:-}" ]; then
    if msb images -q | grep -qx "$IMAGE_REF"; then
        info "Image ${IMAGE_REF} is loaded."
    else
        warn "Image ${IMAGE_REF} is not in the microsandbox cache."
        warn "Load it with: make build  (podman build and msb load)"
        warn "TAU_IMAGE bypasses automatic image management."
    fi
elif msb images -q | grep -qx "$IMAGE_REF"; then
    warn "Image ${IMAGE_REF} is already loaded. Rebuild with: make build"
else
    info "Building image ${IMAGE_REF}..."
    podman build -t "$IMAGE_NAME" "$SCRIPT_DIR"
    info "Loading image into the microsandbox cache..."
    podman save "$IMAGE_NAME" | msb load
    info "Image loaded."
fi

# --- Done ---

echo ""
echo "============================="
echo " Sandbox installed!"
echo "============================="
echo ""
echo " Add this alias to your ~/.bashrc (or ~/.zshrc):"
echo ""
echo "   alias tau-sandbox='${SCRIPT_DIR}/run.sh'"
echo ""
echo " Then use it from any project:"
echo ""
echo "   cd ~/Projects/my-project"
echo "   tau-sandbox tau -p \"Review this codebase\""
echo ""
echo " Other commands:"
echo "   tau-sandbox                       # interactive shell in sandbox"
echo "   tau-sandbox tau                   # start the Tau TUI"
echo "   tau-sandbox npm test              # run any command inside"
echo "   tau-sandbox --reset               # wipe persistent volume for current project"
echo ""
