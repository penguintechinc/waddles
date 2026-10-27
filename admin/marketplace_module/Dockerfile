# Dockerfile for WaddleBot Marketplace Module
# node:20 reached upstream EOL 2026-04-30; bumped to node:24 (same interim pin as hub_module).
FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20

WORKDIR /app

# Install dumb-init and build tools for native modules (bcrypt). perl-base is an
# exact pin to the Debian bookworm security release that clears the trivy
# HIGH/CRITICAL finding (never a blanket `apt-get upgrade`, devops-containers.md).
RUN apt-get update && apt-get install -y --no-install-recommends \
    perl-base=5.36.0-7+deb12u4 \
    dumb-init python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

# Create non-root user
RUN groupadd -r -g 1001 nodejs && \
    useradd -r -u 1001 -g nodejs -s /sbin/nologin nodejs

# Copy backend package files
COPY admin/marketplace_module/backend/package*.json ./

# Install production dependencies only
RUN npm install --only=production

# Remove build tools to reduce image size
RUN apt-get remove -y python3 make g++ \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# The runtime entrypoint is `node src/index.js` -- npm/npx/corepack are never
# invoked after this point. Remove the base image's bundled global npm (and its
# vendored node_modules: tar, brace-expansion, sigstore, ... carry independent CVEs
# unrelated to this app's dependency tree) to close that residual attack surface.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack

# Copy backend source
COPY --chown=nodejs:nodejs admin/marketplace_module/backend/src ./src

# Create log directory and fix ownership
RUN mkdir -p /var/log/waddlebotlog && chown -R nodejs:nodejs /var/log/waddlebotlog /app

# Set environment variables
ENV NODE_ENV=production
ENV PORT=8070

# Switch to non-root user
USER nodejs

# Expose port
EXPOSE 8070

# Health check using native Node.js (python3 removed in build)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD node -e "require('http').get('http://localhost:8070/health', (r) => process.exit(r.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))"

# Start with dumb-init
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "src/index.js"]
