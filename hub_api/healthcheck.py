"""Container HEALTHCHECK probe for hub-api (loopback self-probe, scheme-aware).

When the chart serves HTTPS (``TLS_CERT_FILE`` set) the probe dials ``https://localhost``;
the server certificate names the service FQDN, not ``localhost``, so verification is skipped
for this loopback-only self-check (no credentials are sent). Otherwise it probes plain HTTP.
"""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    """Return 0 when ``/healthz`` answers 200, else 1."""
    tls = bool(os.environ.get("TLS_CERT_FILE"))
    url = f"{'https' if tls else 'http'}://localhost:8204/healthz"
    try:
        resp = httpx.get(url, verify=not tls, timeout=3.0)  # noqa: S501 - loopback self-probe
    except httpx.HTTPError:
        return 1
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
