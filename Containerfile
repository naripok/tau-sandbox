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

# Tau pinned to a commit of the naripok/tau fork (currently the 0.3.10
# release). The sandbox image is the upgrade vehicle:
# rebuild the image (make build) to update Tau or system packages.
# Per-project package images embed a hash of this file and config/;
# changing either invalidates them and triggers an approval-gated rebuild
# on the project's next run.
# Installed into a dedicated venv: Arch's python-pip is PEP 668
# externally-managed, so system-wide pip installs are rejected.
ARG TAU_REF=bc2d5a18d4af5259cf0db8e81d0936a92016d01f
RUN python -m venv /opt/tau && \
    /opt/tau/bin/pip install --no-cache-dir "git+https://github.com/naripok/tau@${TAU_REF}"

# Sandbox user. The microVM is booted with --user 1000:1000 and mounts are
# identity-virtualized by microsandbox: writes land on the host as the host
# user that owns the mounted directory.
RUN useradd -m -u 1000 -s /bin/bash tau

# Static sandbox files: run.sh overlays the current environment reference and
# host-config bootstrap sources read-only at runtime; the entrypoint seeds each
# persistent home once.
RUN mkdir -p /etc/tau-sandbox/bootstrap/tau /etc/tau-sandbox/shared \
      /var/lib/tau-sandbox/sessions /var/lib/tau-sandbox/logs && \
    chown -R tau:tau /var/lib/tau-sandbox
COPY config/APPEND_SYSTEM.md /etc/tau-sandbox/APPEND_SYSTEM.md
COPY config/.bashrc /etc/tau-sandbox/.bashrc

# Tau wrapper: always injects the immutable sandbox reference and uses an
# in-place credential writer for the sole writable host-config file mount.
COPY config/tau-wrapper.py /usr/local/bin/tau

# Entrypoint: initializes the persistent home, sets up the environment, and
# then execs the user command.
COPY config/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/tau /usr/local/bin/entrypoint.sh && \
    find / -xdev -perm /6000 -type f -exec chmod a-s {} +

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
