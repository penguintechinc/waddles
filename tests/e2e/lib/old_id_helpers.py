#!/usr/bin/env python3
"""Helpers for tests/e2e/old-id-scan.sh (kept out of bash for JSON parsing).

Subcommands (all read-only, print one JSON object):
  cm      < kubectl get cm -o json     -> ConfigMap keys holding an old id
  podenv  < kubectl get pods -o json   -> literal container env values holding an old id
  s3      (env S3_ENDPOINT/S3_ACCESS_KEY/S3_SECRET_KEY) -> bucket prefixes (depth<=2) with old ids
Only counts and object NAMES (never values) are emitted.
"""

from __future__ import annotations

import json
import re
import sys

# Left-anchored so a NEW id such as penguintechinc.waddles.core-x never matches.
OLD = re.compile(
    r"(?:^|[^a-z0-9.])waddles\.(?:core\.(?:example|app|service|integration|community)|integrations\.superpenguin)"
)


def cm() -> dict:
    """Scan ConfigMap data for old prefixes."""
    items = json.load(sys.stdin)["items"]
    hits = []
    for it in items:
        for k, v in (it.get("data") or {}).items():
            n = len(OLD.findall(v or ""))
            if n:
                hits.append({"configmap": it["metadata"]["name"], "key": k, "matches": n})
    return {"examined_configmaps": len(items), "hits": hits, "total": sum(h["matches"] for h in hits)}


def podenv() -> dict:
    """Scan literal env values (secretKeyRef values are never read)."""
    items = json.load(sys.stdin)["items"]
    hits, envs = [], 0
    for p in items:
        spec = p["spec"]
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            for e in c.get("env", []) or []:
                envs += 1
                if "value" in e and OLD.search(e["value"] or ""):
                    hits.append({"pod": p["metadata"]["name"], "container": c["name"], "env": e["name"]})
    return {"examined_pods": len(items), "examined_env_vars": envs, "hits": hits, "total": len(hits)}


def s3() -> dict:
    """List bucket prefixes at depth 1-2 whose name carries an old id (read-only)."""
    import os

    import boto3
    from botocore.config import Config

    endpoint = os.environ["S3_ENDPOINT"]
    loopback = re.match(r"https?://(localhost|127\.0\.0\.1)(:|/|$)", endpoint) is not None
    cli = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"], region_name=os.environ.get("AWS_REGION", "us-east-1"),
        verify=not loopback, config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}),
    )
    buckets = [b["Name"] for b in cli.list_buckets()["Buckets"]]
    hits, prefixes, objects = [], 0, 0

    def level(bucket: str, prefix: str, depth: int) -> None:
        nonlocal prefixes, objects
        pager = cli.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
        for page in pager:
            for obj in page.get("Contents", []):
                objects += 1
                if OLD.search("/" + obj["Key"]):
                    hits.append({"bucket": bucket, "prefix": obj["Key"]})
            for cp in page.get("CommonPrefixes", []):
                prefixes += 1
                pre = cp["Prefix"]
                if OLD.search("/" + pre):
                    hits.append({"bucket": bucket, "prefix": pre})
                elif depth < 2:
                    level(bucket, pre, depth + 1)

    for b in buckets:
        level(b, "", 1)
    return {"examined_buckets": len(buckets), "examined_prefixes": prefixes, "examined_objects": objects, "hits": hits, "total": len(hits)}


if __name__ == "__main__":
    print(json.dumps({"cm": cm, "podenv": podenv, "s3": s3}[sys.argv[1]]()))
