#!/bin/bash
set -euo pipefail

# Tau agent sandbox entrypoint — runs as the sandbox user (tau, uid 1000)
# inside a hardware-isolated microsandbox microVM.
#
# Filesystem layout:
#   /workspace                 project bind mount (rw)
#   /home/tau                  persistent named volume
#   /home/tau/.tau/<resources> writable per-project config, bootstrapped once
#                               from host ~/.tau entries when present
#   /etc/tau-sandbox/bootstrap/tau/<resources>
#                               host bootstrap sources (ro)
#   /home/tau/.tau/credentials.json
#                               shared host credential file (rw exception)
#   /home/tau/.tau/sessions    isolated per-project volume (rw)
#   /home/tau/.tau/logs        isolated per-project volume (rw)
#   /home/tau/.tau/trust.json  isolated state in the home volume (rw)
#   /home/tau/.agents          host ~/.agents (ro, when present)
#
# The microVM rootfs uses a disposable writable overlay and is discarded after
# every run. Everything that must survive lives in /workspace or /home/tau.

TAU_HOME=/home/tau
TAU_DIR="$TAU_HOME/.tau"

mkdir -p "$TAU_HOME/.local/bin" "$TAU_DIR" "$TAU_HOME/.agents"

# Seed host Tau defaults into this project's persistent home exactly once.
# The host entries are mounted at an alternate read-only path so Tau can later
# atomically replace its local providers, catalog, settings, and other files.
BOOTSTRAP_DIR=/etc/tau-sandbox/bootstrap/tau
BOOTSTRAP_MARKER="$TAU_DIR/.host-config-bootstrapped"
if [ ! -e "$BOOTSTRAP_MARKER" ]; then
    shopt -s dotglob nullglob
    for source in "$BOOTSTRAP_DIR"/*; do
        name="${source##*/}"
        destination="$TAU_DIR/$name"
        if [ ! -e "$destination" ] && [ ! -L "$destination" ]; then
            cp -a "$source" "$destination"
            chmod -R u+w "$destination"
        fi
    done
    shopt -u dotglob nullglob
    : > "$BOOTSTRAP_MARKER"
fi

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
