FROM archlinux:latest

# Per-project system packages, passed by run.sh when a .opencode-packages file
# exists in the project root. Requires explicit user approval at runtime.
ARG EXTRA_PACKAGES=""

RUN pacman -Syu --noconfirm && \
    pacman -S --noconfirm \
      python python-pip uv nodejs npm git openssh bash which fd ripgrep \
      diffutils gcc make rsync ast-grep curl ca-certificates nss tar \
      chromium ttf-liberation \
      ${EXTRA_PACKAGES} || \
    { echo "" >&2; \
      echo "Error: package installation failed." >&2; \
      echo "Extra packages requested: ${EXTRA_PACKAGES}" >&2; \
      echo "Verify names at https://archlinux.org/packages/ or run 'pacman -Ss <name>'" >&2; \
      exit 1; } && \
    pacman -Scc --noconfirm

# opencode pinned to a release version. The sandbox image is the upgrade
# vehicle: rebuild the image (make build) to update opencode or system
# packages. Per-project package images embed a hash of this file and config/;
# changing either invalidates them and triggers an approval-gated rebuild
# on the project's next run.
# The release asset follows the build architecture (glibc builds; the musl
# variants are for musl systems and never apply to Arch).
ARG OPENCODE_VERSION=1.18.29
# set -o pipefail: a failed curl must fail the RUN, not feed tar an empty stream.
RUN set -o pipefail && case "$(uname -m)" in \
      x86_64) OPENCODE_ARCH=x64 ;; \
      aarch64) OPENCODE_ARCH=arm64 ;; \
      *) echo "Error: unsupported build architecture: $(uname -m)" >&2; exit 1 ;; \
    esac && \
    install -d /opt/opencode/bin && \
    curl -fsSL "https://github.com/sst/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${OPENCODE_ARCH}.tar.gz" \
      | tar -xz -C /opt/opencode/bin

# Sandbox user. The microVM is booted with --user 1000:1000 and mounts are
# identity-virtualized by microsandbox: writes land on the host as the host
# user that owns the mounted directory.
RUN useradd -m -u 1000 -s /bin/bash opencode

# Static sandbox files: run.sh overlays the current environment reference and
# host-config bootstrap sources read-only at runtime; the entrypoint seeds each
# persistent home once.
RUN mkdir -p /etc/opencode-sandbox/bootstrap/opencode \
      /var/lib/opencode-sandbox/sessions /var/lib/opencode-sandbox/logs && \
    chown -R opencode:opencode /var/lib/opencode-sandbox
COPY config/APPEND_SYSTEM.md /etc/opencode-sandbox/APPEND_SYSTEM.md
COPY config/opencode.json /etc/opencode-sandbox/opencode.json
COPY config/.bashrc /etc/opencode-sandbox/.bashrc

# opencode wrapper: always merges the immutable sandbox config, which injects
# the sandbox context document as an instructions entry.
COPY config/opencode-wrapper.sh /usr/local/bin/opencode

# Browser CLI and runtime-CA synchronization for the image's Chromium.
COPY config/browser.mjs /usr/local/bin/browser
COPY config/sync-browser-ca.sh /usr/local/bin/sync-browser-ca

# Entrypoint: initializes the persistent home, sets up the environment, and
# then execs the user command.
COPY config/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/opencode /usr/local/bin/entrypoint.sh /usr/local/bin/browser \
      /usr/local/bin/sync-browser-ca && \
    find / -xdev -perm /6000 -type f -exec chmod a-s {} +

ENV HOME=/home/opencode
ENV TERM=xterm-256color
ENV COLORTERM=truecolor
ENV USER=opencode

# The VM runs as this user by default; run.sh also passes --user 1000:1000
# explicitly so the identity does not depend on image defaults.
USER opencode

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/bash"]
