# PI WEB — the web UI for the Pi Coding Agent (#1357).
#
# Upstream (jmfederico/pi-web) publishes an **npm package only**: its `docker/`
# tree is a Dockerfile you build yourself, and no OCI image is pushed anywhere.
# So this repo builds one, like it does for the engine and the gatekeeper, and
# `templates/pi-web` pulls `ghcr.io/mdopp/solaris-pi-web:latest`.
#
# Two stages for the same reason upstream splits them: node-pty is a native
# module and needs a compiler, which has no business in the image that ends up
# on the box.

ARG NODE_IMAGE=docker.io/library/node:22-trixie

FROM ${NODE_IMAGE} AS package

# Pinned, not `latest`: a template redeploy must not silently move the agent
# runtime under a running session. Bump it here, in one commit, on purpose.
ARG PI_WEB_VERSION=1.202609.0

ENV NPM_CONFIG_UPDATE_NOTIFIER=false

# `--include=peer` pulls @earendil-works/pi-coding-agent, which is a peer
# dependency rather than a dependency: without it pi-web installs but has no
# agent to run. npm here runs install scripts by default, so node-pty builds
# itself — upstream's `--allow-scripts=node-pty` is an npm 12 flag with no
# equivalent on this npm and would abort the install.
RUN npm install -g --omit=dev --include=peer --no-audit --no-fund \
      "@jmfederico/pi-web@${PI_WEB_VERSION}" \
 && ln -sf "$(npm root -g)/@jmfederico/pi-web/node_modules/.bin/pi" \
           "$(npm prefix -g)/bin/pi" \
 && npm cache clean --force

FROM docker.io/library/node:22-trixie-slim AS runtime

# git and a real shell are the point of the product — an agent session is a
# shell in a repository. tini reaps the processes those sessions leave behind.
# python3 runs `pi-web-project` below: the box's other post-deploy/CLI tooling is
# stdlib Python and covered by the repo's ruff + pytest gates, which a second
# language in this one file would leave untested.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git less procps python3 ripgrep tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=package /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=package /usr/local/bin /usr/local/bin

# Pi ships no MCP, so a session reads this box through a CLI instead: one
# delegated read-only ServiceBay token per project (#1395).
COPY --chmod=0755 pi_web_project.py /usr/local/bin/pi-web-project

# Everything durable lives under /data, which the pod mounts from the host:
# the PI WEB state (project registry, session archives, the sessiond socket)
# and the Pi agent directory that carries auth, settings and models.json —
# the file the post-deploy writes to point Pi at llama-server on this box.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/data/home \
    XDG_CONFIG_HOME=/data/config \
    PI_WEB_HOST=0.0.0.0 \
    PI_WEB_PORT=8504 \
    PI_WEB_DATA_DIR=/data/pi-web \
    PI_WEB_SESSIOND_SOCKET=/data/pi-web/sessiond.sock \
    PI_CODING_AGENT_DIR=/data/pi-agent \
    SHELL=/bin/bash \
    TERM=xterm-256color

WORKDIR /workspace
USER node

EXPOSE 8504

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pi-web-server"]
