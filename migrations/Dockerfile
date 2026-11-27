FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3

WORKDIR /app

# OS-package CVE fix: exact pin of Debian bookworm security release for
# linux-pam (CVE-2025-6020, trivy HIGH gate). Drop once base digest is rebuilt upstream.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpam-modules=1.5.2-6+deb12u3 \
    libpam-modules-bin=1.5.2-6+deb12u3 \
    libpam-runtime=1.5.2-6+deb12u3 \
    libpam0g=1.5.2-6+deb12u3 \
    && rm -rf /var/lib/apt/lists/*

# Hash-locked: alembic revisions 0062/0075 import cryptography (AESGCM).
# Same pinned version as hub_api (migrations/requirements.txt).
COPY migrations/requirements.txt /tmp/migrations-requirements.txt
ARG CACHE_EPOCH=1
# (DL3042 below: the pip cache is a BuildKit cache mount, used only with --require-hashes)
# hadolint ignore=DL3042
RUN --mount=type=cache,id=waddles-pip-migrations-v${CACHE_EPOCH},target=/root/.cache/pip,sharing=locked \
    pip install --require-hashes -r /tmp/migrations-requirements.txt && \
    rm -f /tmp/migrations-requirements.txt

# Copy Alembic configuration
COPY alembic.ini .
COPY alembic/ alembic/

# Copy SQLAlchemy models (needed for target_metadata)
# Create __init__.py files so Python recognizes the nested package path
COPY libs/flask_core/ libs/flask_core/
RUN touch libs/__init__.py libs/flask_core/__init__.py

# Copy legacy SQL migrations (used by baseline migration)
COPY config/postgres/migrations/ config/postgres/migrations/

# RBAC matrix (spec D28) -- the normative source the Sec6.10/Sec11.10
# migrations generate GRANT/REVOKE statements from at migration-run time.
COPY config/postgres/rbac-matrix.yaml config/postgres/rbac-matrix.yaml
# Per-service LOGIN role catalog (H-1 / H-3) -- rendered by scripts/db/service_roles.py
# from 0055_per_service_db_roles and migrations/run-alembic.sh's every-run reconcile.
COPY config/postgres/service-roles.yaml config/postgres/service-roles.yaml
COPY scripts/db/ scripts/db/

# Copy migration runner
COPY migrations/run-alembic.sh ./run.sh
# Create non-root user
RUN chmod +x ./run.sh && \
    useradd --create-home --shell /bin/bash appuser

# Set proper permissions for migrations
# Remove base image's own bundled pip + ensurepip wheel: pip vendors an
# outdated msgpack (GHSA-6v7p-g79w-8964) and pkg_resources/setuptools
# (CVE-2025-47273) that the upstream python:3.13-slim-bookworm rebuild
# hasn't picked up yet. Neither pip nor its vendor tree is imported by
# any service at runtime (installs happen at build time via uv/pip
# above), so removing them is a real fix, not a suppression.
RUN chown -R appuser:appuser /app && \
    rm -rf /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/ensurepip

USER appuser

ENTRYPOINT ["./run.sh"]
