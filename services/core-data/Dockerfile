# Multi-stage build for core-data service
# Combines 4 modules: analytics, engagement, reputation, labels

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS builder

WORKDIR /app

# Copy requirements
COPY services/core-data/requirements.txt .
# Pin pip itself: base image ships 26.0.1 (PYSEC-2026-2875/2876/196/3721, fixed <=26.1.2/26.2)
RUN pip install --no-cache-dir --upgrade "pip==26.2.1" && \
    pip install --no-cache-dir -r requirements.txt

# Copy all 4 module source directories
COPY core/analytics_core_module /app/core/analytics_core_module
COPY core/engagement_module /app/core/engagement_module
COPY core/reputation_module /app/core/reputation_module
COPY core/labels_core_module /app/core/labels_core_module

# Copy shared libs
COPY libs /app/libs

# Install flask_core from libs
RUN pip install --no-cache-dir /app/libs/flask_core

# Copy combined app files
COPY services/core-data/app.py /app/app.py
COPY services/core-data/config.py /app/config.py
COPY services/core-data/engagement_bp.py /app/engagement_bp.py

# Final stage
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

WORKDIR /app

# Copy installed packages and app from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# This runtime stage's own base image ships an older bundled pip
# (pip-26.0.1.dist-info) that the COPY above doesn't remove (COPY merges,
# it doesn't delete stale destination files) -- drop it so only the
# builder's patched pip metadata (26.2.1) remains.
RUN find /usr/local/lib/python3.13/site-packages -maxdepth 1 -name 'pip-*.dist-info' ! -name 'pip-26.2.1.dist-info' -exec rm -rf {} +

# Create non-root user for security
RUN useradd -m -u 1000 app && chown -R app:app /app
# Remove base image's own bundled pip + ensurepip wheel: pip vendors an
# outdated msgpack (GHSA-6v7p-g79w-8964) and pkg_resources/setuptools
# (CVE-2025-47273) that the upstream python:3.13-slim-bookworm rebuild
# hasn't picked up yet. Neither pip nor its vendor tree is imported by
# any service at runtime (installs happen at build time via uv/pip
# above), so removing them is a real fix, not a suppression.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/ensurepip

USER app

# Health check using native Python (no curl/wget)
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8040/healthz').read()" || exit 1

# Expose port 8040 (unified port for all 4 modules)
EXPOSE 8040

# Run the combined service
CMD ["python3", "-m", "hypercorn", "app:app", "--bind", "0.0.0.0:8040"]
