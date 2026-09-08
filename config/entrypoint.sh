#!/bin/bash
set -euo pipefail

# opencode agent sandbox entrypoint — runs as the sandbox user
# (opencode, uid 1000) inside a hardware-isolated microsandbox microVM.
#
# Filesystem layout:
#   /workspace                 project bind mount (rw)
#   /home/opencode             persistent named volume
#   /home/opencode/.config/opencode/<resources>
#                               writable per-project config, refreshed from
#                               host config on every start
#   /etc/opencode-sandbox/bootstrap/opencode/<resources>
#                               host bootstrap sources (ro)
#   /home/opencode/.local/share/opencode/auth.json
#                               project-local credential file in the home
#                               volume (rw)
#   /home/opencode/.local/share/opencode/storage
#                               link to the isolated per-project session
#                               storage volume (rw)
#   /home/opencode/.local/share/opencode/log
#                               link to the isolated per-project log
#                               volume (rw)
#
# The microVM rootfs uses a disposable writable overlay and is discarded after
# every run. Durable state lives in /workspace, /home/opencode, or the
# persistent storage and log mounts linked from the home.
#
# All scratch globals, loop targets, temporaries, and function locals use the
# reserved OPENCODE_SANDBOX_ENTRYPOINT_ prefix. The reservation keeps every
# piece of entrypoint scratch state in one namespace, separate from guest
# project secret values.

OPENCODE_SANDBOX_ENTRYPOINT_HOME=/home/opencode
OPENCODE_SANDBOX_ENTRYPOINT_CONFIG_DIR="$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.config/opencode"
OPENCODE_SANDBOX_ENTRYPOINT_DATA_DIR="$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.local/share/opencode"

mkdir -p "$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.local/bin" "$OPENCODE_SANDBOX_ENTRYPOINT_DATA_DIR" "$OPENCODE_SANDBOX_ENTRYPOINT_CONFIG_DIR"

# Microsandbox prepares nested mount targets as root before starting this
# unprivileged entrypoint. Keep those mounts outside the persistent home and
# link them into opencode's expected paths only after the home directories
# exist with uid 1000.
link_volume_dir() {
    local OPENCODE_SANDBOX_ENTRYPOINT_BACKING="$1"
    local OPENCODE_SANDBOX_ENTRYPOINT_TARGET="$2"
    local OPENCODE_SANDBOX_ENTRYPOINT_VOLUME_NAME="${OPENCODE_SANDBOX_ENTRYPOINT_TARGET##*/}"
    local OPENCODE_SANDBOX_ENTRYPOINT_LEGACY="$OPENCODE_SANDBOX_ENTRYPOINT_DATA_DIR/.${OPENCODE_SANDBOX_ENTRYPOINT_VOLUME_NAME}.pre-link"
    if [ -L "$OPENCODE_SANDBOX_ENTRYPOINT_TARGET" ]; then
        if [ "$(readlink "$OPENCODE_SANDBOX_ENTRYPOINT_TARGET")" != "$OPENCODE_SANDBOX_ENTRYPOINT_BACKING" ]; then
            echo "Error: $OPENCODE_SANDBOX_ENTRYPOINT_TARGET points to an unexpected location." >&2
            exit 1
        fi
        return
    fi
    if [ -e "$OPENCODE_SANDBOX_ENTRYPOINT_TARGET" ]; then
        if [ ! -d "$OPENCODE_SANDBOX_ENTRYPOINT_TARGET" ]; then
            echo "Error: cannot initialize persistent state link $OPENCODE_SANDBOX_ENTRYPOINT_TARGET." >&2
            exit 1
        fi
        if ! rmdir "$OPENCODE_SANDBOX_ENTRYPOINT_TARGET" 2>/dev/null; then
            if [ -e "$OPENCODE_SANDBOX_ENTRYPOINT_LEGACY" ]; then
                echo "Error: both $OPENCODE_SANDBOX_ENTRYPOINT_TARGET and $OPENCODE_SANDBOX_ENTRYPOINT_LEGACY require migration." >&2
                exit 1
            fi
            mv "$OPENCODE_SANDBOX_ENTRYPOINT_TARGET" "$OPENCODE_SANDBOX_ENTRYPOINT_LEGACY"
        fi
    fi
    if [ -d "$OPENCODE_SANDBOX_ENTRYPOINT_LEGACY" ]; then
        # A real session or log directory could exist from a run outside this
        # entrypoint's layout. Merge its contents without replacing canonical
        # volume data.
        cp -Rn "$OPENCODE_SANDBOX_ENTRYPOINT_LEGACY/." "$OPENCODE_SANDBOX_ENTRYPOINT_BACKING/"
    fi
    ln -s "$OPENCODE_SANDBOX_ENTRYPOINT_BACKING" "$OPENCODE_SANDBOX_ENTRYPOINT_TARGET"
    if [ -d "$OPENCODE_SANDBOX_ENTRYPOINT_LEGACY" ]; then
        rm -rf "$OPENCODE_SANDBOX_ENTRYPOINT_LEGACY" 2>/dev/null || true
    fi
}

link_volume_dir /var/lib/opencode-sandbox/sessions "$OPENCODE_SANDBOX_ENTRYPOINT_DATA_DIR/storage"
link_volume_dir /var/lib/opencode-sandbox/logs "$OPENCODE_SANDBOX_ENTRYPOINT_DATA_DIR/log"

# Refresh host-managed opencode config on every start. Sources are mounted at
# an alternate read-only path, then copied into writable project-local paths
# so opencode can still use atomic writes during the session. Host config is
# authoritative at startup; local entries that have never come from the host
# remain untouched. Generated install artifacts (plugin dependency trees and
# their lockfiles) are never synced: opencode installs its own per directory.
OPENCODE_SANDBOX_ENTRYPOINT_BOOTSTRAP_DIR=/etc/opencode-sandbox/bootstrap/opencode
OPENCODE_SANDBOX_ENTRYPOINT_SYNC_MANIFEST="$OPENCODE_SANDBOX_ENTRYPOINT_CONFIG_DIR/.host-config-synced"
OPENCODE_SANDBOX_ENTRYPOINT_LEGACY_BOOTSTRAP_MARKER="$OPENCODE_SANDBOX_ENTRYPOINT_CONFIG_DIR/.host-config-bootstrapped"

# Remove resources that were synchronized previously but have since been
# removed from the host. Validate manifest names because the sandbox can write
# this file between starts.
if [ -f "$OPENCODE_SANDBOX_ENTRYPOINT_SYNC_MANIFEST" ]; then
    # Stream one record at a time: each iteration reads exactly one
    # manifest record, and an unterminated final record (no trailing
    # newline) is ignored. The shape is safe under `set -u` on old Bash,
    # and the command-local IFS= is read control syntax, not scratch
    # state.
    while IFS= read -r OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME; do
        [ -n "$OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME" ] || continue
        [ "$OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME" = "${OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME##*/}" ] || continue
        case "$OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME" in
            .|..|auth.json|node_modules|package.json|package-lock.json|bun.lock|.gitignore|.host-config-bootstrapped|.host-config-synced)
                continue
                ;;
        esac
        if [ ! -e "$OPENCODE_SANDBOX_ENTRYPOINT_BOOTSTRAP_DIR/$OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME" ] && [ ! -L "$OPENCODE_SANDBOX_ENTRYPOINT_BOOTSTRAP_DIR/$OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME" ]; then
            rm -rf -- "$OPENCODE_SANDBOX_ENTRYPOINT_CONFIG_DIR/$OPENCODE_SANDBOX_ENTRYPOINT_SYNCED_NAME"
        fi
    done < "$OPENCODE_SANDBOX_ENTRYPOINT_SYNC_MANIFEST"
fi

OPENCODE_SANDBOX_ENTRYPOINT_MANIFEST_TMP="$(mktemp "$OPENCODE_SANDBOX_ENTRYPOINT_CONFIG_DIR/.host-config-synced.XXXXXX")"
shopt -s dotglob nullglob
for OPENCODE_SANDBOX_ENTRYPOINT_SOURCE in "$OPENCODE_SANDBOX_ENTRYPOINT_BOOTSTRAP_DIR"/*; do
    OPENCODE_SANDBOX_ENTRYPOINT_NAME="${OPENCODE_SANDBOX_ENTRYPOINT_SOURCE##*/}"
    case "$OPENCODE_SANDBOX_ENTRYPOINT_NAME" in
        auth.json|node_modules|package.json|package-lock.json|bun.lock|.gitignore|.host-config-bootstrapped|.host-config-synced)
            continue
            ;;
    esac
    OPENCODE_SANDBOX_ENTRYPOINT_DESTINATION="$OPENCODE_SANDBOX_ENTRYPOINT_CONFIG_DIR/$OPENCODE_SANDBOX_ENTRYPOINT_NAME"
    rm -rf -- "$OPENCODE_SANDBOX_ENTRYPOINT_DESTINATION"
    cp -a "$OPENCODE_SANDBOX_ENTRYPOINT_SOURCE" "$OPENCODE_SANDBOX_ENTRYPOINT_DESTINATION"
    chmod -R u+w "$OPENCODE_SANDBOX_ENTRYPOINT_DESTINATION"
    printf '%s\n' "$OPENCODE_SANDBOX_ENTRYPOINT_NAME" >> "$OPENCODE_SANDBOX_ENTRYPOINT_MANIFEST_TMP"
done
shopt -u dotglob nullglob
rm -rf -- "$OPENCODE_SANDBOX_ENTRYPOINT_SYNC_MANIFEST"
mv "$OPENCODE_SANDBOX_ENTRYPOINT_MANIFEST_TMP" "$OPENCODE_SANDBOX_ENTRYPOINT_SYNC_MANIFEST"
rm -rf -- "$OPENCODE_SANDBOX_ENTRYPOINT_LEGACY_BOOTSTRAP_MARKER"

# First-run shell setup. The volume persists, so these run once per project.
if [ ! -f "$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.bashrc" ]; then
    cp /etc/opencode-sandbox/.bashrc "$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.bashrc"
fi
if [ ! -f "$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.bash_profile" ]; then
    printf 'if [ -f ~/.bashrc ]; then\n  . ~/.bashrc\nfi\n' > "$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.bash_profile"
fi

# User-level package-manager defaults. Idempotent; set every boot.
# npm lifecycle scripts can run arbitrary code during install; disabled
# unless a user opts in with `npm install --ignore-scripts=false`.
npm config set prefix "$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.local"
npm config set ignore-scripts true

# Environment for the agent.
export HOME="$OPENCODE_SANDBOX_ENTRYPOINT_HOME"
export SHELL=/bin/bash
export TERM="${TERM:-xterm-256color}"
export COLORTERM=truecolor
export USER=opencode
export LOGNAME=opencode
export PATH="$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
export PYTHONUSERBASE="$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.local"
export NPM_CONFIG_PREFIX="$OPENCODE_SANDBOX_ENTRYPOINT_HOME/.local"
export PIP_USER=1

# The sandbox image is the upgrade vehicle; skip opencode's auto-update.
export OPENCODE_DISABLE_AUTOUPDATE=true

exec "$@"
