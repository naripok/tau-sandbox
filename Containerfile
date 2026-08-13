FROM archlinux:latest

# Per-project system packages, passed by run.sh when a .tau-packages file
# exists in the project root. Requires explicit user approval at runtime.
ARG EXTRA_PACKAGES=""

RUN pacman -Syu --noconfirm && \
    pacman -S --noconfirm \
      python python-pip uv nodejs npm git openssh bash which fd ripgrep \
      diffutils gcc make rsync ast-grep curl ca-certificates tar \
      ${EXTRA_PACKAGES} || \
    { echo "" >&2; \
      echo "Error: package installation failed." >&2; \
      echo "Extra packages requested: ${EXTRA_PACKAGES}" >&2; \
      echo "Verify names at https://archlinux.org/packages/ or run 'pacman -Ss <name>'" >&2; \
      exit 1; } && \
    pacman -Scc --noconfirm

# Pinned Tau release. The sandbox image is the upgrade vehicle:
# rebuild the image (make build) to update Tau or system packages.
# Installed into a dedicated venv: Arch's python-pip is PEP 668
# externally-managed, so system-wide pip installs are rejected.
ARG TAU_VERSION=0.3.9
RUN python -m venv /opt/tau && \
    /opt/tau/bin/pip install --no-cache-dir "tau-ai==${TAU_VERSION}" && \
    ln -s /opt/tau/bin/tau /usr/local/bin/tau

# Sandbox user. The microMV is booted with --user 1000:1000 and mounts are
# identity-virtualized by microsandbox: writes land on the host as the host
# user that owns the mounted directory.
RUN useradd -m -u 1000 -s /bin/bash tau

# Static sandbox files: the agent environment reference and shell config,
# copied into the persistent volume on first boot by the entrypoint.
RUN mkdir -p /etc/tau-sandbox
COPY config/APPEND_SYSTEM.md /etc/tau-sandbox/APPEND_SYSTEM.md
COPY config/.bashrc /etc/tau-sandbox/.bashrc

# Entrypoint: syncs host config into the volume, sets up the environment,
# then execs the user command.
COPY config/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

ENV HOME=/home/tau
ENV TERM=xterm-256color
ENV COLORTERM=truecolor
ENV USER=tau

# The VM runs as this user by default; run.sh also passes --user 1000:1000
# explicitly so the identity does not depend on image defaults.
USER tau

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/bash"]
