# Disposable "clean Ubuntu" (24.04 by default, 26.04 with UBUNTU=26.04) used by tests/integration/run_e2e.sh.
# Mirrors a fresh server/desktop: python3 present, a regular user with sudo,
# nothing Frappe-related installed. git and ca-certificates stand in for "the user cloned the repo".
ARG UBUNTU=24.04
FROM ubuntu:${UBUNTU}

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

# Optional: Node.js for testing the npm launcher (`npx frappe-setup-cli`), installed the way a
# user would get it from nodejs.org. Empty NODE_VERSION = no Node.js (default).
ARG NODE_VERSION=""
RUN if [ -n "$NODE_VERSION" ]; then \
        python3 -c "import urllib.request; urllib.request.urlretrieve('https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.gz', '/tmp/node.tgz')" \
        && tar -xzf /tmp/node.tgz -C /usr/local --strip-components=1 \
        && rm /tmp/node.tgz && node --version && npm --version; \
    fi

# Ubuntu 24.04+ images ship a default 'ubuntu' user (uid 1000); use a dedicated one.
RUN useradd --create-home --shell /bin/bash dev \
    && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev

USER dev
WORKDIR /home/dev
ENV LANG=C.UTF-8
