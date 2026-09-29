# Multi-stage build for combined streaming trigger receiver service
# Supports: Twitch, YouTube Live, Kick streaming receivers
# Port: 8101

FROM python:3.13-slim-bookworm@sha256:f13a6b7565175da40695e8109f64cbc4d2e65f4c9ef2e3b321c3a44fa3c06fe7 AS builder
WORKDIR /app

# Copy requirements for pip dependency resolution
COPY services/trigger-streaming/requirements.txt .
# Pin pip itself: base image ships 26.0.1 (PYSEC-2026-2875/2876/196/3721, fixed <=26.1.2/26.2)
RUN pip install --no-cache-dir --upgrade "pip==26.2.1" && \
    pip install --no-cache-dir -r requirements.txt

# Copy and install flask_core from shared libs
COPY libs/flask_core /app/libs/flask_core
RUN pip install --no-cache-dir /app/libs/flask_core

# Copy all three module directories (maintain original structure)
COPY trigger/receiver/twitch_module twitch_module/
COPY trigger/receiver/youtube_live_module youtube_live_module/
COPY trigger/receiver/kick_module_flask kick_module_flask/
COPY services/trigger-streaming/app.py app.py
COPY services/trigger-streaming/config.py config.py

# Runtime stage
FROM python:3.13-slim-bookworm@sha256:f13a6b7565175da40695e8109f64cbc4d2e65f4c9ef2e3b321c3a44fa3c06fe7
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# This runtime stage's own base image ships an older bundled pip
# (pip-26.0.1.dist-info) that the COPY above doesn't remove (COPY merges,
# it doesn't delete stale destination files) -- drop it so only the
# builder's patched pip metadata (26.2.1) remains.
RUN find /usr/local/lib/python3.13/site-packages -maxdepth 1 -name 'pip-*.dist-info' ! -name 'pip-26.2.1.dist-info' -exec rm -rf {} +

# Copy application files
COPY --from=builder /app /app

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Health check using native Python (no curl)
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import httpx; httpx.get('http://localhost:8101/healthz')" || exit 1

# Expose port 8101
EXPOSE 8101

# Run Quart app
CMD ["python3", "-m", "hypercorn", "app:app", "--bind", "0.0.0.0:8101"]
