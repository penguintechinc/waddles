"""Pytest bootstrap for workflow_core_module's tests.

workflow_core_module isn't installed as a package (it's a standalone
control-plane directory run via `hypercorn app:app`, same shape as
`core/svc_action`/`core/svc_streaming`) -- so its own directory has to be
put on sys.path explicitly for `from config import Config` / `from
controllers.workflow_api import ...` / `from services...` to resolve.
"""

from __future__ import annotations

import os
import subprocess
import sys

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MODULE_ROOT)

# `Config.SECRET_KEY = require_secret_key()` runs at import time
# (config.py module body) -- setting a real-looking value here keeps
# collection from depending on `require_secret_key`'s pytest-detection
# fallback being evaluated in exactly this process for every import order.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-workflow-core-module-tests")


def _generate_proto_stubs() -> None:
    """Generate workflow_pb2*.py from proto/workflow.proto if missing.

    services/grpc_handler.py does `sys.path.insert(0, ".../proto")` then a
    bare `import workflow_pb2` / `import workflow_pb2_grpc` -- those
    modules only exist if protoc has been run. The Dockerfile generates
    them at image-build time (`-I./proto --python_out=./proto
    --grpc_python_out=./proto ./proto/workflow.proto`); this mirrors that
    exact invocation so the test environment matches the built image
    instead of drifting from it. Idempotent and regenerates whenever the
    .proto source is newer than the last generated stub, so editing the
    .proto during development doesn't require a manual step or a stale
    checked-in copy.
    """
    proto_dir = os.path.join(MODULE_ROOT, "proto")
    proto_src = os.path.join(proto_dir, "workflow.proto")
    pb2 = os.path.join(proto_dir, "workflow_pb2.py")
    pb2_grpc = os.path.join(proto_dir, "workflow_pb2_grpc.py")

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


_generate_proto_stubs()
sys.path.insert(0, os.path.join(MODULE_ROOT, "proto"))
