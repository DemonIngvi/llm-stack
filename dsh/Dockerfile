# DeepSeek Harness (dsh) Web UI, pinned by package-lock.json.
# dsh runs model-generated commands, so it lives in a container and sees
# only /workspace and its own /data/dsh.
FROM node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates git python3 ripgrep \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/dsh
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY --chmod=0755 entrypoint.sh /usr/local/bin/dsh-entrypoint
COPY bridge/ /opt/bridge/

ENV PATH=/opt/dsh/node_modules/.bin:$PATH
USER node
WORKDIR /workspace
ENTRYPOINT ["dsh-entrypoint"]
