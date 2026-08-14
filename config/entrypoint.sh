#!/bin/bash
set -euo pipefail

# Tau agent sandbox entrypoint — runs as the sandbox user (tau, uid 1000)
# inside a hardware-isolated microsandbox microVM.
#
# Filesystem layout:
#   /workspace          project bind mount (rw)
#   /home/tau           persistent named volume
#   /home/tau/.tau      host ~/.tau      (shared read-write; may be absent)
#   /home/tau/.agents   host ~/.agents   (shared read-write; may be absent)
#
# run.sh mounts the host config dirs into the sandbox home, so the agent
# reads and writes the same Tau config as the host: login tokens
# (credentials.json), skills, prompts, themes, sessions, and logs. Writes
# reach the host through microsandbox identity virtualization.
#
# The microVM rootfs is ephemeral — it is discarded after every run.
# Everything that must survive lives under /home/tau or in the shared
# config mounts.

TAU_HOME=/home/tau
TAU_DIR="$TAU_HOME/.tau"

mkdir -p "$TAU_HOME/.local/bin" "$TAU_DIR" "$TAU_HOME/.agents"

# Appended system prompt describing this sandbox. Refreshed every start so
# it stays in sync with the repo (when /workspace is the tau-sandbox checkout)
# and with the image copy baked into /etc/tau-sandbox.
#
# The target is the agent's config dir (~/.tau), which is the host's own
# ~/.tau when run.sh mounted it there.
if [ -f /workspace/config/APPEND_SYSTEM.md ]; then
    cp /workspace/config/APPEND_SYSTEM.md "$TAU_DIR/APPEND_SYSTEM.md"
elif [ -f /etc/tau-sandbox/APPEND_SYSTEM.md ]; then
    cp /etc/tau-sandbox/APPEND_SYSTEM.md "$TAU_DIR/APPEND_SYSTEM.md"
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
