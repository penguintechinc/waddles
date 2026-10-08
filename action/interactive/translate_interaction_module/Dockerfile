# Use Python 3.12 — numpy==1.26.4 and fasttext-wheel have no Python 3.13 wheels (build from source fails)
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

# uv replaces pip for dependency installation (pinned by digest)
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv

WORKDIR /app

# Install shared flask_core library
COPY libs/flask_core /app/libs/flask_core
RUN uv pip install --system --no-cache /app/libs/flask_core

# Install dependencies. setuptools (fasttext-wheel's build-time dep) is pinned
# and hashed in requirements.txt, so no separate pre-install step is needed.
COPY action/interactive/translate_interaction_module/requirements.txt /app/
RUN uv pip install --system --no-cache -r requirements.txt

# Copy module source
COPY action/interactive/translate_interaction_module /app/

# Generate gRPC stubs at build time
# Use -I . so protoc sees proto/ as a package prefix, generating
# 'from proto import translate_interaction_pb2' (not bare import)
RUN python -m grpc_tools.protoc \
    -I . \
    --python_out=. \
    --grpc_python_out=. \
    proto/translate_interaction.proto

# Non-root user
RUN mkdir -p /var/log/waddlebotlog && \
    groupadd -r waddlebot && useradd -r -g waddlebot -m waddlebot && \
    chown -R waddlebot:waddlebot /app /var/log/waddlebotlog

USER waddlebot

EXPOSE 8033 50033

CMD ["python", "app.py"]
