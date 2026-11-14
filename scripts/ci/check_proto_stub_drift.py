#!/usr/bin/env python3
"""Fail when the committed hub-api Python gRPC stubs drift from the `.proto` sources.

For every `proto/waddles/**/*.proto` this compiles a descriptor set with `grpc_tools.protoc`
and compares it with the serialized descriptor embedded in the committed `*_pb2.py`, plus checks
that every service method exists on the committed `*_pb2_grpc.py` servicer and stub. Comparing
descriptors (not file text) keeps the check independent of ruff formatting of the stubs.

Zero protos examined, a missing stub module, or any mismatch exits non-zero; counts are printed
(critical-rules.md Verification Integrity). Regenerate with `scripts/compile_protos_v1.sh`.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
from pathlib import Path

from google.protobuf import descriptor_pb2

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTO_DIR = REPO_ROOT / "proto"
PB_DIR = REPO_ROOT / "hub_api" / "grpc_internal" / "pb"


def _strip_json_names(message: descriptor_pb2.DescriptorProto) -> None:
    """Clear `json_name` (protoc fills it in descriptor sets; the embedded pb2 copy omits it)."""
    for field in (*message.field, *message.extension):
        field.ClearField("json_name")
    for nested in message.nested_type:
        _strip_json_names(nested)


def compile_descriptors(protos: list[Path]) -> dict[str, descriptor_pb2.FileDescriptorProto]:
    """Compile `protos` into a name -> FileDescriptorProto map using the pinned protoc."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "set.pb"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "grpc_tools.protoc",
                f"-I{PROTO_DIR}",
                f"--descriptor_set_out={out}",
                *[str(p) for p in protos],
            ],
            check=True,
        )
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(out.read_bytes())
    for file_proto in fds.file:
        for message in file_proto.message_type:
            _strip_json_names(message)
        for field in file_proto.extension:
            field.ClearField("json_name")
    return {f.name: f for f in fds.file}


def main() -> int:
    """Run the drift check and return the process exit code."""
    protos = sorted((PROTO_DIR / "waddles").rglob("*.proto"))
    if not protos:
        print("check_proto_stub_drift: FAIL - 0 .proto files examined", file=sys.stderr)
        return 1
    expected = compile_descriptors(protos)
    sys.path.insert(0, str(PB_DIR))
    failures: list[str] = []
    for proto in protos:
        rel = proto.relative_to(PROTO_DIR).as_posix()
        module_name = rel[: -len(".proto")].replace("/", ".")
        try:
            pb2 = importlib.import_module(f"{module_name}_pb2")
        except ImportError as exc:
            failures.append(f"{rel}: stub module missing ({exc})")
            continue
        committed = descriptor_pb2.FileDescriptorProto()
        committed.ParseFromString(pb2.DESCRIPTOR.serialized_pb)
        if committed != expected[rel]:
            failures.append(f"{rel}: committed *_pb2.py descriptor differs from the .proto")
            continue
        if not expected[rel].service:
            continue
        try:
            grpc_mod = importlib.import_module(f"{module_name}_pb2_grpc")
        except ImportError as exc:
            failures.append(f"{rel}: *_pb2_grpc.py missing ({exc})")
            continue
        for svc in expected[rel].service:
            servicer = getattr(grpc_mod, f"{svc.name}Servicer", None)
            if servicer is None:
                failures.append(f"{rel}: {svc.name}Servicer missing from *_pb2_grpc.py")
                continue
            for method in svc.method:
                if not hasattr(servicer, method.name):
                    failures.append(f"{rel}: {svc.name}.{method.name} missing from *_pb2_grpc.py")
    print(f"check_proto_stub_drift: examined {len(protos)} .proto files, {len(failures)} drifted")
    for line in failures:
        print(f"  DRIFT {line}", file=sys.stderr)
    if failures:
        print("Regenerate with scripts/compile_protos_v1.sh", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
