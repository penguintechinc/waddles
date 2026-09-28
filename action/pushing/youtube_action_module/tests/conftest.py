"""Shared pytest fixtures for this module's test suite.

Ensures MODULE_SECRET_KEY is set to a deterministic test value before
``config.Config`` is imported by any test module, so JWT signing/verification
in the auth interceptor tests is reproducible regardless of the local
environment (some modules default this to an empty string, which PyJWT
correctly refuses to sign with).
"""

from __future__ import annotations

import os
import subprocess
import sys

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault(
    "MODULE_SECRET_KEY", "pytest-only-test-secret-do-not-use-in-production"
)


def _generate_proto_stubs() -> None:
    """Generate youtube_action_pb2*.py from proto/youtube_action.proto if missing.

    services/grpc_handler.py does a bare `from proto import
    youtube_action_pb2, youtube_action_pb2_grpc` at module import time, with
    no try/except -- and services/__init__.py imports grpc_handler
    unconditionally, so simply importing `services.grpc_auth_interceptor`
    (as test_grpc_auth_interceptor.py does) pulls it in. Those stubs only
    exist if protoc has been run; the Dockerfile generates them at
    image-build time (`-I./proto --python_out=./proto
    --grpc_python_out=./proto ./proto/youtube_action.proto`), which this
    mirrors exactly so the test environment matches the built image instead
    of drifting from it. Idempotent and regenerates whenever the .proto
    source is newer than the last generated stub.
    """
    proto_dir = os.path.join(MODULE_ROOT, "proto")
    proto_src = os.path.join(proto_dir, "youtube_action.proto")
    pb2 = os.path.join(proto_dir, "youtube_action_pb2.py")
    pb2_grpc = os.path.join(proto_dir, "youtube_action_pb2_grpc.py")

    stale = (
        not os.path.exists(pb2)
        or not os.path.exists(pb2_grpc)
        or os.path.getmtime(proto_src) > os.path.getmtime(pb2)
    )
    if not stale:
        return

    subprocess.run(  # noqa: S603 -- fixed argv (sys.executable + repo-local
        # paths only), no shell, no untrusted input; mirrors the Dockerfile's
        # own `python -m grpc_tools.protoc` build step.
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{proto_dir}",
            f"--python_out={proto_dir}",
            f"--grpc_python_out={proto_dir}",
            proto_src,
        ],
        check=True,
        cwd=MODULE_ROOT,
    )

    # Dockerfile also rewrites the grpc stub's absolute `import
    # youtube_action_pb2` to a relative `from . import youtube_action_pb2`
    # so it resolves as part of the `proto` package rather than requiring
    # `proto/` itself on sys.path -- mirror that here too.
    with open(pb2_grpc, encoding="utf-8") as f:
        content = f.read()
    patched = content.replace(
        "import youtube_action_pb2", "from . import youtube_action_pb2", 1
    )
    if patched != content:
        with open(pb2_grpc, "w", encoding="utf-8") as f:
            f.write(patched)


_generate_proto_stubs()
sys.path.insert(0, MODULE_ROOT)
