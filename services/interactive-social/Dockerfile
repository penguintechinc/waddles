FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS builder
WORKDIR /app
COPY services/interactive-social/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy and install flask_core from shared libs
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY services/interactive-social/app.py .
COPY action/interactive/alias_interaction_module ./alias_interaction_module
COPY action/interactive/shoutout_interaction_module ./shoutout_interaction_module
COPY action/interactive/presence_module ./presence_module
COPY action/interactive/quote_interaction_module ./quote_interaction_module

EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import httpx; resp = httpx.get('http://localhost:8010/healthz'); exit(0 if resp.status_code == 200 else 1)" || exit 1

RUN groupadd -r appuser && useradd -r -g appuser -m appuser && chown -R appuser:appuser /app

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

CMD ["python3", "app.py"]
