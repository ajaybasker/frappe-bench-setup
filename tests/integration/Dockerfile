# Disposable "clean Ubuntu 24.04" used by tests/integration/run_e2e.sh.
# Mirrors a fresh server/desktop: python3 present, a regular user with sudo,
# nothing Frappe-related installed. git and ca-certificates stand in for "the user cloned the repo".
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 sudo git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Optional (test infrastructure only): switch APT to another mirror for the test run, e.g.
# https://mirrors.edge.kernel.org/ubuntu  (HTTPS works once ca-certificates is installed above;
# some networks block individual files over plain HTTP).
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu/\?|$APT_MIRROR/|g; s|http://security.ubuntu.com/ubuntu/\?|$APT_MIRROR/|g" \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi

# Ubuntu 24.04 images ship a default 'ubuntu' user (uid 1000); use a dedicated one.
RUN useradd --create-home --shell /bin/bash dev \
    && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev

USER dev
WORKDIR /home/dev
ENV LANG=C.UTF-8
