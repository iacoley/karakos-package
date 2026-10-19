# One pinned Node major for BOTH the dashboard build stage and the runtime
# image, so native modules (better-sqlite3, sqlite3) built in the first load in
# the second by construction. Both bases are Debian bookworm (same glibc).
ARG NODE_MAJOR=22

# The dashboard source is dashboard/ in this repo. This stage installs its
# dependencies from the lockfile (npm ci), builds it (next build) and keeps
# only what `next start` needs. Native modules (better-sqlite3, sqlite3) build
# here and load in the runtime image.
FROM node:${NODE_MAJOR}-bookworm-slim AS dashboard-build
# Compiler toolchain for native modules when no prebuild matches; this stage is
# discarded, nothing here reaches the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build \
    && rm -rf .next/cache \
    && npm prune --omit=dev
# Fail the stage if a source map or a native module is missing from the output.
RUN [ -z "$(find .next -name '*.map' -print -quit)" ] \
    && node -e "require('better-sqlite3'); require('sqlite3'); console.log('native ok')"

# The package's own files, minus the dashboard source: the runtime image gets
# the built dashboard from the stage above, not the raw tree.
FROM node:${NODE_MAJOR}-bookworm-slim AS workspace-src
COPY . /src
RUN rm -rf /src/dashboard

FROM python:3.11-slim-bookworm

# Re-declare (an ARG before the first FROM is not visible inside a stage).
ARG NODE_MAJOR

# Install runtime tools (tini for PID 1, git/curl/jq for runtime use)
# build-essential is needed because some Python deps (PyStemmer via fastembed)
# ship no aarch64 wheel and have to compile from source. We purge it after
# pip install to keep the final image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl tini jq build-essential \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI for builds (gh pr create/view), from GitHub's own apt repository
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install supervisord
RUN pip install --no-cache-dir supervisor

# Install Node.js for Claude CLI and the dashboard (same major as the build stage)
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Purge build toolchain after pip is done with it — keeps image lean
RUN apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user. The Claude Code CLI refuses
# `--dangerously-skip-permissions` when the running uid is 0, so the agent
# subprocess MUST run as a non-root user.  uid 1000 is a common host uid which
# makes bind-mounted credentials (~/.claude) line up cleanly for most users.
ARG KARAKOS_UID=1000
ARG KARAKOS_GID=1000
RUN groupadd --system --gid ${KARAKOS_GID} karakos \
    && useradd --system --uid ${KARAKOS_UID} --gid ${KARAKOS_GID} \
        --home-dir /home/karakos --create-home --shell /bin/bash karakos

# Bake the embedding model weights (as the build user) so a container with no
# network still has them. KARAKOS_BAKE_EMBED_MODEL=0 skips the download; recall
# then falls back to keyword mode.
ARG KARAKOS_BAKE_EMBED_MODEL=1
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN install -d -o karakos -g karakos /opt/fastembed
USER karakos
RUN if [ "$KARAKOS_BAKE_EMBED_MODEL" = "1" ]; then \
        python3 -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')"; \
    fi
USER root

WORKDIR /workspace
# WORKDIR creates the directory as root. Hand it to karakos so the user can
# write into it (entrypoint.sh runs `git init` there, agents log to it, etc.).
RUN chown karakos:karakos /workspace

# Use --chown on COPY rather than a post-hoc `chown -R` so the workspace's
# millions of node_modules files don't have to be rewritten in a new layer.
COPY --chown=karakos:karakos --from=dashboard-build /app/.next dashboard/.next
COPY --chown=karakos:karakos --from=dashboard-build /app/node_modules dashboard/node_modules
COPY --chown=karakos:karakos --from=dashboard-build /app/public dashboard/public
COPY --chown=karakos:karakos --from=dashboard-build /app/package.json dashboard/package.json
COPY --chown=karakos:karakos --from=dashboard-build /app/next.config.mjs dashboard/next.config.mjs
COPY --chown=karakos:karakos --from=workspace-src /src/ ./

# Dashboard runtime config (variables documented in dashboard/README.md).
# The dashboard shares this container and reads agent-server.db from the data volume.
ENV KARAKOS_REGISTRY_PATH=/workspace/config/agents.yaml \
    AGENT_SERVER_DB_PATH=/workspace/data/memory/agent-server.db \
    WORKSPACE_ROOT=/workspace

# Create data directories owned by karakos so volume mounts get the right
# ownership when first created. data/ gets NO subdirectories here: Docker copies
# an image directory's contents into a new named volume, and a non-empty data/
# looks like an unstamped 1.x install to the schema guard (exit 78 loop). The
# entrypoint creates data/messages, memory and health after the stamp check.
RUN install -d -o karakos -g karakos \
        data \
        logs logs/agent-streams logs/session-summaries \
        inbox

# Strip CRLF line endings from shell scripts. Belt-and-suspenders for Windows
# checkouts where git's autocrlf may have rewritten LF -> CRLF, which breaks
# shebangs inside the Linux container (env: 'bash\r': No such file, exit 127).
# .gitattributes also forces LF on these files at the repo level.
RUN find /workspace/bin -type f \( -name '*.sh' -o -name 'kara' \) \
        -exec sed -i 's/\r$//' {} +

USER karakos
ENTRYPOINT ["/usr/bin/tini", "--", "/workspace/bin/entrypoint.sh"]
