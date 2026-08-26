#!/bin/bash
set -euo pipefail

# Tau agent sandbox entrypoint — runs as the sandbox user (tau, uid 1000)
# inside a hardware-isolated microsandbox microVM.
#
# Filesystem layout:
#   /workspace                 project bind mount (rw)
#   /home/tau                  persistent named volume
#   /home/tau/.tau/<resources> writable per-project config, refreshed from
#                               host ~/.tau entries on every start
#   /etc/tau-sandbox/bootstrap/tau/<resources>
#                               host bootstrap sources (ro)
#   /home/tau/.tau/credentials.json
#                               project-local credential file in the home volume (rw)
#   /home/tau/.tau/sessions    link to isolated per-project volume (rw)
#   /home/tau/.tau/logs        link to isolated per-project volume (rw)
#   /home/tau/.tau/trust.json  isolated state in the home volume (rw)
#   /home/tau/.agents          host ~/.agents (ro, when present)
#
# The microVM rootfs uses a disposable writable overlay and is discarded after
# every run. Durable state lives in /workspace, /home/tau, or the persistent
# session and log mounts linked from the home.
#
# All scratch globals, loop targets, temporaries, and function locals use the
# reserved TAU_ENTRYPOINT_ prefix. The reservation keeps every piece of
# entrypoint scratch state in one namespace, separate from guest project
# secret values.

TAU_ENTRYPOINT_HOME=/home/tau
TAU_ENTRYPOINT_DIR="$TAU_ENTRYPOINT_HOME/.tau"

mkdir -p "$TAU_ENTRYPOINT_HOME/.local/bin" "$TAU_ENTRYPOINT_HOME/.agents"

# Recover a fresh home initialized by the previous nested-mount layout. In that
# layout agentd could create ~/.tau as root before this uid-1000 process began.
# Keep the inaccessible directory for inspection; reset eventually removes it.
if [ -d "$TAU_ENTRYPOINT_DIR" ] && [ ! -w "$TAU_ENTRYPOINT_DIR" ]; then
    TAU_ENTRYPOINT_LEGACY_TAU_DIR="$TAU_ENTRYPOINT_HOME/.tau.msb-root-owned"
    if [ -e "$TAU_ENTRYPOINT_LEGACY_TAU_DIR" ]; then
        echo "Error: both $TAU_ENTRYPOINT_DIR and $TAU_ENTRYPOINT_LEGACY_TAU_DIR are unusable." >&2
        exit 1
    fi
    mv "$TAU_ENTRYPOINT_DIR" "$TAU_ENTRYPOINT_LEGACY_TAU_DIR"
fi
mkdir -p "$TAU_ENTRYPOINT_DIR"

# Microsandbox prepares nested mount targets as root before starting this
# unprivileged entrypoint. Keep those mounts outside the persistent home and
# link them into Tau's expected paths only after ~/.tau exists with uid 1000.
link_volume_dir() {
    local TAU_ENTRYPOINT_BACKING="$1"
    local TAU_ENTRYPOINT_TARGET="$2"
    local TAU_ENTRYPOINT_VOLUME_NAME="${TAU_ENTRYPOINT_TARGET##*/}"
    local TAU_ENTRYPOINT_LEGACY="$TAU_ENTRYPOINT_DIR/.$TAU_ENTRYPOINT_VOLUME_NAME.pre-link"
    if [ -L "$TAU_ENTRYPOINT_TARGET" ]; then
        if [ "$(readlink "$TAU_ENTRYPOINT_TARGET")" != "$TAU_ENTRYPOINT_BACKING" ]; then
            echo "Error: $TAU_ENTRYPOINT_TARGET points to an unexpected location." >&2
            exit 1
        fi
        return
    fi
    if [ -e "$TAU_ENTRYPOINT_TARGET" ]; then
        if [ ! -d "$TAU_ENTRYPOINT_TARGET" ]; then
            echo "Error: cannot initialize persistent state link $TAU_ENTRYPOINT_TARGET." >&2
            exit 1
        fi
        if ! rmdir "$TAU_ENTRYPOINT_TARGET" 2>/dev/null; then
            if [ -e "$TAU_ENTRYPOINT_LEGACY" ]; then
                echo "Error: both $TAU_ENTRYPOINT_TARGET and $TAU_ENTRYPOINT_LEGACY require migration." >&2
                exit 1
            fi
            mv "$TAU_ENTRYPOINT_TARGET" "$TAU_ENTRYPOINT_LEGACY"
        fi
    fi
    if [ -d "$TAU_ENTRYPOINT_LEGACY" ]; then
        # Older launch layouts could leave real session/log directories under
        # ~/.tau. Merge their contents without replacing canonical volume data.
        cp -Rn "$TAU_ENTRYPOINT_LEGACY/." "$TAU_ENTRYPOINT_BACKING/"
    fi
    ln -s "$TAU_ENTRYPOINT_BACKING" "$TAU_ENTRYPOINT_TARGET"
    if [ -d "$TAU_ENTRYPOINT_LEGACY" ]; then
        rm -rf "$TAU_ENTRYPOINT_LEGACY" 2>/dev/null || true
    fi
}

link_volume_dir /var/lib/tau-sandbox/sessions "$TAU_ENTRYPOINT_DIR/sessions"
link_volume_dir /var/lib/tau-sandbox/logs "$TAU_ENTRYPOINT_DIR/logs"

# Credentials stay project-local: the host file is never mounted, and the
# entrypoint never touches credentials.json. Tau reads and writes it directly
# in the persistent home volume with its stock atomic writer.

# Refresh host-managed Tau config on every start. Sources are mounted at an
# alternate read-only path, then copied into writable project-local paths so
# Tau can still use atomic replacement during the session. Host config is
# authoritative at startup; local entries that have never come from the host
# remain untouched.
TAU_ENTRYPOINT_BOOTSTRAP_DIR=/etc/tau-sandbox/bootstrap/tau
TAU_ENTRYPOINT_SYNC_MANIFEST="$TAU_ENTRYPOINT_DIR/.host-config-synced"
TAU_ENTRYPOINT_LEGACY_BOOTSTRAP_MARKER="$TAU_ENTRYPOINT_DIR/.host-config-bootstrapped"

# Remove resources that were synchronized previously but have since been
# removed from the host. Validate manifest names because the sandbox can write
# this file between starts.
if [ -f "$TAU_ENTRYPOINT_SYNC_MANIFEST" ]; then
    # Stream one record at a time: each iteration reads exactly one
    # manifest record, and an unterminated final record (no trailing
    # newline) is ignored. The shape is safe under `set -u` on old Bash,
    # and the command-local IFS= is read control syntax, not scratch
    # state.
    while IFS= read -r TAU_ENTRYPOINT_SYNCED_NAME; do
        [ -n "$TAU_ENTRYPOINT_SYNCED_NAME" ] || continue
        [ "$TAU_ENTRYPOINT_SYNCED_NAME" = "${TAU_ENTRYPOINT_SYNCED_NAME##*/}" ] || continue
        case "$TAU_ENTRYPOINT_SYNCED_NAME" in
            .|..|credentials.json|sessions|logs|trust.json|trust.json.lock|trust.json.pending|.host-config-bootstrapped|.host-config-synced)
                continue
                ;;
        esac
        if [ ! -e "$TAU_ENTRYPOINT_BOOTSTRAP_DIR/$TAU_ENTRYPOINT_SYNCED_NAME" ] && [ ! -L "$TAU_ENTRYPOINT_BOOTSTRAP_DIR/$TAU_ENTRYPOINT_SYNCED_NAME" ]; then
            rm -rf -- "$TAU_ENTRYPOINT_DIR/$TAU_ENTRYPOINT_SYNCED_NAME"
        fi
    done < "$TAU_ENTRYPOINT_SYNC_MANIFEST"
fi

TAU_ENTRYPOINT_MANIFEST_TMP="$(mktemp "$TAU_ENTRYPOINT_DIR/.host-config-synced.XXXXXX")"
shopt -s dotglob nullglob
for TAU_ENTRYPOINT_SOURCE in "$TAU_ENTRYPOINT_BOOTSTRAP_DIR"/*; do
    TAU_ENTRYPOINT_NAME="${TAU_ENTRYPOINT_SOURCE##*/}"
    case "$TAU_ENTRYPOINT_NAME" in
        credentials.json|sessions|logs|trust.json|trust.json.lock|trust.json.pending|.host-config-bootstrapped|.host-config-synced)
            continue
            ;;
    esac
    TAU_ENTRYPOINT_DESTINATION="$TAU_ENTRYPOINT_DIR/$TAU_ENTRYPOINT_NAME"
    rm -rf -- "$TAU_ENTRYPOINT_DESTINATION"
    cp -a "$TAU_ENTRYPOINT_SOURCE" "$TAU_ENTRYPOINT_DESTINATION"
    chmod -R u+w "$TAU_ENTRYPOINT_DESTINATION"
    printf '%s\n' "$TAU_ENTRYPOINT_NAME" >> "$TAU_ENTRYPOINT_MANIFEST_TMP"
done
shopt -u dotglob nullglob
rm -rf -- "$TAU_ENTRYPOINT_SYNC_MANIFEST"
mv "$TAU_ENTRYPOINT_MANIFEST_TMP" "$TAU_ENTRYPOINT_SYNC_MANIFEST"
rm -rf -- "$TAU_ENTRYPOINT_LEGACY_BOOTSTRAP_MARKER"

# First-run shell setup. The volume persists, so these run once per project.
if [ ! -f "$TAU_ENTRYPOINT_HOME/.bashrc" ]; then
    cp /etc/tau-sandbox/.bashrc "$TAU_ENTRYPOINT_HOME/.bashrc"
fi
if [ ! -f "$TAU_ENTRYPOINT_HOME/.bash_profile" ]; then
    printf 'if [ -f ~/.bashrc ]; then\n  . ~/.bashrc\nfi\n' > "$TAU_ENTRYPOINT_HOME/.bash_profile"
fi

# User-level package-manager defaults. Idempotent; set every boot.
# npm lifecycle scripts can run arbitrary code during install; disabled
# unless a user opts in with `npm install --ignore-scripts=false`.
npm config set prefix "$TAU_ENTRYPOINT_HOME/.local"
npm config set ignore-scripts true

# Environment for the agent.
export HOME="$TAU_ENTRYPOINT_HOME"
export SHELL=/bin/bash
export TERM="${TERM:-xterm-256color}"
export COLORTERM=truecolor
export USER=tau
export LOGNAME=tau
export PATH="$TAU_ENTRYPOINT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
export PYTHONUSERBASE="$TAU_ENTRYPOINT_HOME/.local"
export NPM_CONFIG_PREFIX="$TAU_ENTRYPOINT_HOME/.local"
export PIP_USER=1

# The sandbox image is the upgrade vehicle; skip Tau's online version check.
export TAU_NO_UPDATE_CHECK=1

exec "$@"
