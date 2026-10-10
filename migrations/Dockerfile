FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

WORKDIR /app

# Install migration dependencies
RUN pip install --no-cache-dir \
    alembic>=1.13 \
    sqlalchemy>=2.0 \
    psycopg2-binary \
    flask-sqlalchemy \
    flask-security-too \
    "PyYAML>=6.0.2,<7.0.0"

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
# from 0049_per_service_db_roles and migrations/run-alembic.sh's every-run reconcile.
COPY config/postgres/service-roles.yaml config/postgres/service-roles.yaml
COPY scripts/db/ scripts/db/

# Copy migration runner
COPY migrations/run-alembic.sh ./run.sh
RUN chmod +x ./run.sh

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Set proper permissions for migrations
RUN chown -R appuser:appuser /app

# Remove base image's own bundled pip + ensurepip wheel: pip vendors an
# outdated msgpack (GHSA-6v7p-g79w-8964) and pkg_resources/setuptools
# (CVE-2025-47273) that the upstream python:3.13-slim-bookworm rebuild
# hasn't picked up yet. Neither pip nor its vendor tree is imported by
# any service at runtime (installs happen at build time via uv/pip
# above), so removing them is a real fix, not a suppression.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/ensurepip

USER appuser

ENTRYPOINT ["./run.sh"]
