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
#                               link to shared host credential file (rw exception)
#   /home/tau/.tau/sessions    link to isolated per-project volume (rw)
#   /home/tau/.tau/logs        link to isolated per-project volume (rw)
#   /home/tau/.tau/trust.json  isolated state in the home volume (rw)
#   /home/tau/.agents          host ~/.agents (ro, when present)
#
# The microVM rootfs uses a disposable writable overlay and is discarded after
# every run. Durable state lives in /workspace, /home/tau, or the persistent
# session and log mounts linked from the home.

TAU_HOME=/home/tau
TAU_DIR="$TAU_HOME/.tau"

mkdir -p "$TAU_HOME/.local/bin" "$TAU_HOME/.agents"

# Recover a fresh home initialized by the previous nested-mount layout. In that
# layout agentd could create ~/.tau as root before this uid-1000 process began.
# Keep the inaccessible directory for inspection; reset eventually removes it.
if [ -d "$TAU_DIR" ] && [ ! -w "$TAU_DIR" ]; then
    LEGACY_TAU_DIR="$TAU_HOME/.tau.msb-root-owned"
    if [ -e "$LEGACY_TAU_DIR" ]; then
        echo "Error: both $TAU_DIR and $LEGACY_TAU_DIR are unusable." >&2
        exit 1
    fi
    mv "$TAU_DIR" "$LEGACY_TAU_DIR"
fi
mkdir -p "$TAU_DIR"

# Microsandbox prepares nested mount targets as root before starting this
# unprivileged entrypoint. Keep those mounts outside the persistent home and
# link them into Tau's expected paths only after ~/.tau exists with uid 1000.
link_volume_dir() {
    local backing="$1"
    local target="$2"
    local name="${target##*/}"
    local legacy="$TAU_DIR/.$name.pre-link"
    if [ -L "$target" ]; then
        if [ "$(readlink "$target")" != "$backing" ]; then
            echo "Error: $target points to an unexpected location." >&2
            exit 1
        fi
        return
    fi
    if [ -e "$target" ]; then
        if [ ! -d "$target" ]; then
            echo "Error: cannot initialize persistent state link $target." >&2
            exit 1
        fi
        if ! rmdir "$target" 2>/dev/null; then
            if [ -e "$legacy" ]; then
                echo "Error: both $target and $legacy require migration." >&2
                exit 1
            fi
            mv "$target" "$legacy"
        fi
    fi
    if [ -d "$legacy" ]; then
        # Older launch layouts could leave real session/log directories under
        # ~/.tau. Merge their contents without replacing canonical volume data.
        cp -Rn "$legacy/." "$backing/"
    fi
    ln -s "$backing" "$target"
    if [ -d "$legacy" ]; then
        rm -rf "$legacy" 2>/dev/null || true
    fi
}

link_volume_dir /var/lib/tau-sandbox/sessions "$TAU_DIR/sessions"
link_volume_dir /var/lib/tau-sandbox/logs "$TAU_DIR/logs"

SHARED_CREDENTIALS=/etc/tau-sandbox/shared/credentials.json
CREDENTIALS_LINK="$TAU_DIR/credentials.json"
LOCAL_CREDENTIALS_BACKUP="$TAU_DIR/.sandbox-local-credentials.json"
if [ "${TAU_SANDBOX_SHARED_CREDENTIALS:-0}" = "1" ]; then
    if [ -L "$CREDENTIALS_LINK" ]; then
        if [ "$(readlink "$CREDENTIALS_LINK")" != "$SHARED_CREDENTIALS" ]; then
            echo "Error: $CREDENTIALS_LINK points to an unexpected location." >&2
            exit 1
        fi
    else
        if [ -s "$CREDENTIALS_LINK" ] && [ ! -e "$LOCAL_CREDENTIALS_BACKUP" ]; then
            mv "$CREDENTIALS_LINK" "$LOCAL_CREDENTIALS_BACKUP"
        else
            rm -f "$CREDENTIALS_LINK"
        fi
        ln -s "$SHARED_CREDENTIALS" "$CREDENTIALS_LINK"
    fi
elif [ -L "$CREDENTIALS_LINK" ] && [ "$(readlink "$CREDENTIALS_LINK")" = "$SHARED_CREDENTIALS" ]; then
    rm "$CREDENTIALS_LINK"
    if [ -e "$LOCAL_CREDENTIALS_BACKUP" ]; then
        mv "$LOCAL_CREDENTIALS_BACKUP" "$CREDENTIALS_LINK"
    fi
fi

# Refresh host-managed Tau config on every start. Sources are mounted at an
# alternate read-only path, then copied into writable project-local paths so
# Tau can still use atomic replacement during the session. Host config is
# authoritative at startup; local entries that have never come from the host
# remain untouched.
BOOTSTRAP_DIR=/etc/tau-sandbox/bootstrap/tau
SYNC_MANIFEST="$TAU_DIR/.host-config-synced"
LEGACY_BOOTSTRAP_MARKER="$TAU_DIR/.host-config-bootstrapped"

# Remove resources that were synchronized previously but have since been
# removed from the host. Validate manifest names because the sandbox can write
# this file between starts.
if [ -f "$SYNC_MANIFEST" ]; then
    while IFS= read -r old_name; do
        [ -n "$old_name" ] || continue
        [ "$old_name" = "${old_name##*/}" ] || continue
        case "$old_name" in
            .|..|credentials.json|sessions|logs|trust.json|trust.json.lock|trust.json.pending|.host-config-bootstrapped|.host-config-synced)
                continue
                ;;
        esac
        if [ ! -e "$BOOTSTRAP_DIR/$old_name" ] && [ ! -L "$BOOTSTRAP_DIR/$old_name" ]; then
            rm -rf -- "$TAU_DIR/$old_name"
        fi
    done < "$SYNC_MANIFEST"
fi

manifest_tmp="$(mktemp "$TAU_DIR/.host-config-synced.XXXXXX")"
shopt -s dotglob nullglob
for source in "$BOOTSTRAP_DIR"/*; do
    name="${source##*/}"
    case "$name" in
        credentials.json|sessions|logs|trust.json|trust.json.lock|trust.json.pending|.host-config-bootstrapped|.host-config-synced)
            continue
            ;;
    esac
    destination="$TAU_DIR/$name"
    rm -rf -- "$destination"
    cp -a "$source" "$destination"
    chmod -R u+w "$destination"
    printf '%s\n' "$name" >> "$manifest_tmp"
done
shopt -u dotglob nullglob
rm -rf -- "$SYNC_MANIFEST"
mv "$manifest_tmp" "$SYNC_MANIFEST"
rm -rf -- "$LEGACY_BOOTSTRAP_MARKER"

# First-run shell setup. The volume persists, so these run once per project.
if [ ! -f "$TAU_HOME/.bashrc" ]; then
    cp /etc/tau-sandbox/.bashrc "$TAU_HOME/.bashrc"
fi
if [ ! -f "$TAU_HOME/.bash_profile" ]; then
    printf 'if [ -f ~/.bashrc ]; then\n  . ~/.bashrc\nfi\n' > "$TAU_HOME/.bash_profile"
fi

# User-level package-manager defaults. Idempotent; set every boot.
# npm lifecycle scripts can run arbitrary code during install; disabled
# unless a user opts in with `npm install --ignore-scripts=false`.
npm config set prefix "$TAU_HOME/.local"
npm config set ignore-scripts true

# Environment for the agent.
export HOME="$TAU_HOME"
export SHELL=/bin/bash
export TERM="${TERM:-xterm-256color}"
export COLORTERM=truecolor
export USER=tau
export LOGNAME=tau
export PATH="$TAU_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
export PYTHONUSERBASE="$TAU_HOME/.local"
export NPM_CONFIG_PREFIX="$TAU_HOME/.local"
export PIP_USER=1

# The sandbox image is the upgrade vehicle; skip Tau's online version check.
export TAU_NO_UPDATE_CHECK=1

exec "$@"
