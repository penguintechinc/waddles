"""Operator CLI: publish the connector service bundles (wasm + signed sidecar) to the bucket.

Run as `python3 -m cli.publish_connector_bundles` from hub-api's `/app` (the core-bundle-seeder
image, which carries the reproducibly-built connector wasm under `/core-bundles/`), normally via
`make publish-connector-bundles ENV=dal2alpha` (scripts/publish-connector-bundles.sh).

Secrets come from env/files only, never argv: `BUNDLE_SIGNING_PRIVATE_KEY_FILE` (or
`BUNDLE_SIGNING_PRIVATE_KEY`) + `BUNDLE_SIGNING_KEY_ID`, and `S3_ENDPOINT_URL`/`S3_ACCESS_KEY_ID`/
`S3_SECRET_ACCESS_KEY`/`BUNDLE_BUCKET_NAME`. Prints the digests + a Helm values snippet to stdout
(logs go to stderr). Exit 0 only when every requested connector was published.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from services import connector_publish_service as svc
from services.errors import ApiError

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_dir = Path(os.environ.get("CORE_BUNDLES_DIR", "/core-bundles"))
    parser.add_argument("--bundles-dir", type=Path, default=default_dir)
    parser.add_argument(
        "--catalog", type=Path, default=None, help="default: <bundles-dir>/core-bundles.yaml"
    )
    parser.add_argument("--platforms", default=",".join(svc.CONNECTOR_PLATFORMS))
    return parser


async def _run(args: argparse.Namespace) -> int:
    catalog = args.catalog or args.bundles_dir / "core-bundles.yaml"
    platforms = tuple(p.strip() for p in args.platforms.split(",") if p.strip())
    signer = svc.signer_from_env()
    artifacts = svc.load_connector_artifacts(catalog, args.bundles_dir, platforms)
    published = await svc.publish_connectors(artifacts, signer)
    if len(published) != len(platforms):
        raise ApiError(
            f"published {len(published)} of {len(platforms)} connectors",
            500,
            "connector_publish_incomplete",
        )
    for item in published:
        print(f"{item.platform}\t{item.digest}\t{item.component_key}\t{item.key_id}")
    print(
        f"\n# published {len(published)}/{len(platforms)} connectors; add to the env values file:"
    )
    print(svc.values_snippet(published))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point; any failure prints the error to stderr and exits non-zero."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ApiError as exc:
        print(f"publish-connector-bundles: FAIL: {exc.message} ({exc.code})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
