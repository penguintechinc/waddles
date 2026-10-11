# Multi-stage Dockerfile for WaddleBot Hub Module
# node:20 reached upstream EOL 2026-04-30; bumped to node:24 (Active LTS;
# org target is node:26, not yet released — 24 is the sanctioned interim).
# Stage 1: Build frontend
FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS frontend-build

WORKDIR /build/admin/hub_module/frontend

# Copy frontend package files
COPY admin/hub_module/frontend/package*.json ./

# Install the exact locked dependency graph from public npm without running
# arbitrary lifecycle scripts, then rebuild the one approved native dependency.
# @penguintechinc packages are published to npmjs.com, so no registry auth is
# needed and no token ever enters the build.
RUN npm ci --ignore-scripts && npm rebuild esbuild

# Copy frontend source
COPY admin/hub_module/frontend/ ./

# Build frontend
RUN npm run build

# Stage 2: Production image
FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20

WORKDIR /app

# OS-package CVE fix: pin the Debian bookworm security release that clears the
# HIGH/CRITICAL perl-base findings the pinned node:24-bookworm-slim digest still
# ships (CVE-2026-13221 et al., trivy gate). Explicit exact pin, never a blanket
# `apt-get upgrade` (devops-containers.md). Drop the pin once the base digest is
# rebuilt upstream with this version. Then dumb-init, curl for healthcheck, and
# build tools for native modules (bcrypt).
RUN apt-get update && apt-get install -y --no-install-recommends \
    perl-base=5.36.0-7+deb12u4 \
    dumb-init curl make g++ \
    && rm -rf /var/lib/apt/lists/*


# Create non-root user
RUN groupadd -g 1001 nodejs && \
    useradd -u 1001 -g nodejs -s /bin/bash nodejs

# Copy backend package files
COPY admin/hub_module/backend/package*.json ./

# Install the exact production dependency graph without running arbitrary
# lifecycle scripts, then rebuild the one approved native dependency.
RUN npm ci --omit=dev --ignore-scripts && npm rebuild bcrypt

# Remove build tools to reduce image size (keep curl for healthcheck)
RUN apt-get purge -y make g++ && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# The runtime entrypoint is `node src/index.js` — npm/npx/corepack are never
# invoked after this point. Remove the base image's bundled global npm (and
# its own vendored node_modules, which carry independent CVEs unrelated to
# this app's package-lock.json) to close that residual attack surface.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack

# Copy backend source
COPY --chown=nodejs:nodejs admin/hub_module/backend/src ./src

# Copy built frontend from build stage
COPY --chown=nodejs:nodejs --from=frontend-build /build/admin/hub_module/frontend/dist ./public

# Create log directory and fix ownership
RUN mkdir -p /var/log/waddlebotlog && chown -R nodejs:nodejs /var/log/waddlebotlog /app

# Set environment variables
ENV NODE_ENV=production
ENV PORT=8060

# Switch to non-root user
USER nodejs

# Expose port
EXPOSE 8060

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8060/health || exit 1

# Start with dumb-init
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "src/index.js"]
