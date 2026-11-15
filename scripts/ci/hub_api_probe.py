#!/usr/bin/env python3
"""Count-verified HTTP readiness probe: GET each path until it returns 200 or attempts run out.

Used by the dal2alpha deploy smoke gate against a port-forwarded hub-api, so the runner needs
only create pods/portforward (no pods/exec). Stdlib only: nothing to install on the runner.
Prints the examined count; exits non-zero on zero examined or on any endpoint that never returned 200.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable


def probe_once(url: str, timeout: float) -> int:
    """Return the HTTP status of one GET, or 0 when the connection itself fails or times out."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - URL is caller-supplied local port-forward
            return int(resp.status)
    except urllib.error.HTTPError as exc:  # must precede URLError (HTTPError subclasses it)
        return int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def probe_path(
    base_url: str,
    path: str,
    attempts: int,
    delay: float,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int]:
    """Probe one path until it returns 200; return (last_status, attempts_used)."""
    status = 0
    for attempt in range(1, attempts + 1):
        status = probe_once(base_url.rstrip("/") + path, timeout)
        if status == 200:
            return status, attempt
        if attempt < attempts:
            sleep(delay)
    return status, attempts


def main(argv: list[str] | None = None) -> int:
    """Run the probe over every path, print one line per endpoint plus a count summary, return the exit code."""
    parser = argparse.ArgumentParser(description="Probe HTTP endpoints until 200, with counts.")
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:18080")
    parser.add_argument("--paths", nargs="+", required=True, help="absolute paths, e.g. /health /ready")
    parser.add_argument("--attempts", type=int, default=20, help="max GETs per path (>=1)")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between attempts")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    args = parser.parse_args(argv)

    if args.attempts < 1:
        parser.error("--attempts must be >= 1")
    bad_paths = [p for p in args.paths if not p.startswith("/")]
    if bad_paths:
        parser.error(f"paths must start with '/': {bad_paths}")

    examined = ok = failed = 0
    for path in args.paths:
        status, used = probe_path(args.base_url, path, args.attempts, args.delay, args.timeout)
        examined += 1
        verdict = "PASS" if status == 200 else "FAIL"
        if status == 200:
            ok += 1
        else:
            failed += 1
        print(f"GET {path} status={status} attempts={used} {verdict}")

    print(f"endpoints examined={examined} ok={ok} failed={failed}")
    if examined == 0:
        print("::error::zero endpoints examined", file=sys.stderr)
        return 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
