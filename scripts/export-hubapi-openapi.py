#!/usr/bin/env python3
"""Export hub-api's quart-schema generated OpenAPI document to openapi/v1.yaml.

Builds the app via create_app() (no server, no DB connect) and dumps the same
provider.schema() that backs the gated /openapi/v1.json route.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "openapi" / "v1.yaml"


def main() -> int:
    """Generate the spec and write it; fail loudly on an empty path set."""
    os.environ.setdefault("SECRET_KEY", secrets.token_hex(24))
    os.environ.setdefault("ENVIRONMENT", "development")
    sys.path.insert(0, str(ROOT / "hub_api"))
    from app import create_app  # noqa: PLC0415

    schema = create_app().extensions["QUART_SCHEMA"].openapi_provider.schema()
    schema = json.loads(json.dumps(schema))
    paths = schema.get("paths", {})
    for item in paths.values():
        for op in item.values():
            if isinstance(op, dict) and not op.get("responses"):
                # Routes without @validate_response declare no shape; OAS 3
                # requires >=1 response, so say "undocumented" explicitly.
                op["responses"] = {"default": {"description": "Undocumented response"}}
    seen: dict[str, int] = {}
    for item in paths.values():
        for op in item.values():
            if isinstance(op, dict) and "operationId" in op:
                seen[op["operationId"]] = seen.get(op["operationId"], 0) + 1
    for route, item in paths.items():
        for method, op in item.items():
            if isinstance(op, dict) and seen.get(op.get("operationId", ""), 0) > 1:
                # Same handler name in several blueprints: disambiguate by route.
                slug = re.sub(r"[^a-zA-Z0-9]+", "_", route).strip("_")
                op["operationId"] = f"{op['operationId']}__{method}_{slug}"
    if not paths:
        print("ERROR: generated spec has zero paths", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(yaml.safe_dump(schema, sort_keys=True, allow_unicode=True), encoding="utf-8")
    print(f"wrote {OUT} ({len(paths)} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
