"""Operator CLI: idempotently provision an at-rest DEK for every existing tenant.

Run as ``python3 -m cli.backfill_tenant_keys`` from hub-api's ``/app`` (normally via
``make backfill-tenant-keys``). Reads ``DATABASE_URL`` (and the platform KEK / KMS settings the
envelope runtime reads) from the environment -- never argv. Prints one JSON line with the counts of
tenants examined / created / already present; exits 0 only when every tenant has a key and at
least one tenant was examined (zero examined is a failure, not a pass).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from services.bundle_install_dal import build_install_dal
from services.envelope.runtime import build_envelope_runtime
from services.tenant_key_provisioning import TenantKeyProvisioningError, backfill_tenant_keys

logger = logging.getLogger(__name__)


async def _run() -> int:
    """Provision keys for all tenants and print the report; return the process exit code."""
    dal = await build_install_dal(os.environ["DATABASE_URL"], pool_size=2)
    runtime = build_envelope_runtime(dal)
    try:
        report = await backfill_tenant_keys(dal, runtime.service)
    except TenantKeyProvisioningError as exc:
        logger.error("backfill_tenant_keys.failed: %s", exc)
        return 1
    finally:
        await runtime.aclose()
        await dal.close()
    sys.stdout.write(
        json.dumps(
            {
                "examined": report.examined,
                "created": len(report.created),
                "already_present": len(report.already_present),
            }
        )
        + "\n"
    )
    return 0


def main() -> None:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
