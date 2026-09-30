"""hub-api internal gRPC server package (waddles.hub.internal.v1).

Generated stubs under `pb/` import as `waddles.hub.internal.v1.*_pb2`
(grpc_tools.protoc's default package-mirrors-path behavior against
`proto/waddles/hub/internal/v1/*.proto`) -- this module puts `pb/` on
`sys.path` once, at first import, so those absolute imports resolve
without vendoring the generated tree under a `hub_api`-prefixed package
name (which would fight the Rust side's build.rs, generating the exact
same `waddles.hub.internal.v1` package from the same .proto files).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PB_ROOT = Path(__file__).resolve().parent / "pb"
if str(_PB_ROOT) not in sys.path:
    sys.path.insert(0, str(_PB_ROOT))
