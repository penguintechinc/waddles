#!/usr/bin/env python3
"""Dump hub-api's live Quart url_map to JSON (no server, no DB).

Why: the OpenAPI spec is generated from this same map, so diffing the two is
how the sweep finds routes that exist in code but are missing from the spec.
Usage: PYTHONPATH=libs/flask_core:libs:hub_api python dump_url_map.py OUT.json
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys

os.environ.setdefault("SESSION_MAC_KEY", secrets.token_hex(24))
os.environ.setdefault("ENVIRONMENT", "development")

from app import create_app  # noqa: E402  (needs hub_api on PYTHONPATH)

_CONVERTER = re.compile(r"<(?:[^:>]+:)?([^>]+)>")


def main(out: str) -> int:
    """Write [{rule, methods}] for every non-static rule; fail on an empty map."""
    app = create_app()
    rules = []
    for r in app.url_map.iter_rules():
        methods = sorted(m for m in (r.methods or set()) if m not in ("HEAD", "OPTIONS"))
        if not methods or r.endpoint.endswith("static"):
            continue
        rules.append({"rule": _CONVERTER.sub(r"{\1}", r.rule), "methods": methods, "endpoint": r.endpoint})
    if not rules:
        print("ERROR: url_map is empty", file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)
    print(f"url_map: {len(rules)} rules -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
