#!/usr/bin/env python3
"""Rotate / retire platform secrets through the hub-api admin API (never the database).

This CLI is a thin client of `/api/v1/admin/platform-secrets` -- the SAME endpoints the web UI
uses, so every gate there applies: system-tenant global admin, scope `platform_secrets:admin`,
recent MFA, single-use Idempotency-Key, server-side rate limit and verify window. It has no
database access and holds no keys; it cannot do anything the API would refuse.

    platform_secrets_rotate.py status
    platform_secrets_rotate.py rotate  jwt_signing --reason scheduled --yes
    platform_secrets_rotate.py retire  jwt_signing --version 1 --reason maintenance --yes

Configuration (never on the command line, so nothing lands in shell history or `ps`):

  WADDLES_API_URL       base URL of hub-api (or `--api-url`)
  WADDLES_ADMIN_TOKEN   bearer token of an MFA-fresh admin session, OR
  --token-stdin         read the token from stdin (e.g. `pbpaste | ... --token-stdin`)
  --ca-bundle PATH      extra CA bundle for the TLS connection (certificate checks stay ON)

A token comes from an interactive admin login that completed MFA within the last 5 minutes; a
long-lived token is useless here by design. The token is never printed or logged. Exit codes: 0
ok, 1 refused by the API (its stable error code is shown), 2 usage / configuration, 3 network.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from typing import Any, Final

API_PATH: Final = "/api/v1/admin/platform-secrets"
REASONS: Final = {"scheduled": 1, "compromise": 2, "maintenance": 3}
TOKEN_ENV: Final = "WADDLES_ADMIN_TOKEN"  # nosec B105 -- env var NAME, not a secret
URL_ENV: Final = "WADDLES_API_URL"
TIMEOUT_SECONDS: Final = 30
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_NETWORK = 0, 1, 2, 3


class UsageError(Exception):
    """Bad invocation or missing configuration (message is safe to print)."""


def build_parser() -> argparse.ArgumentParser:
    """The CLI grammar. There is deliberately no `--token` option."""
    parser = argparse.ArgumentParser(
        prog="platform_secrets_rotate.py", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--api-url", help=f"hub-api base URL (default: ${URL_ENV})")
    parser.add_argument(
        "--token-stdin", action="store_true", help="read the bearer token from stdin"
    )
    parser.add_argument(
        "--ca-bundle", help="PEM CA bundle to trust in addition to the system store"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status", help="show every type's versions and states (no secret material)"
    )
    for name, help_text in (
        ("rotate", "mint the next version ACTIVE; the current one becomes verify-only"),
        ("retire", "retire a verify-only version once its verify window has closed"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument(
            "type", help="jwt_signing | bundle_signing | precompile_signing | <code>"
        )
        cmd.add_argument("--reason", choices=sorted(REASONS), required=True)
        cmd.add_argument(
            "--yes", action="store_true", help="confirm (required when not a TTY)"
        )
        cmd.add_argument(
            "--idempotency-key",
            help="UUID to make a retry of THIS request safe (default: a fresh one)",
        )
        if name == "retire":
            cmd.add_argument(
                "--version", type=int, required=True, help="the verify-only version"
            )
    return parser


def read_token(args: argparse.Namespace, environ: dict[str, str], stdin: Any) -> str:
    """The bearer token from stdin or the environment; never from argv."""
    token = (
        stdin.readline().strip()
        if args.token_stdin
        else environ.get(TOKEN_ENV, "").strip()
    )
    if not token:
        raise UsageError(f"no token: set ${TOKEN_ENV} or pass --token-stdin")
    return token


def resolve_url(args: argparse.Namespace, environ: dict[str, str]) -> str:
    """The API base URL (HTTPS required unless it is loopback), without a trailing slash."""
    url = (args.api_url or environ.get(URL_ENV, "")).strip().rstrip("/")
    if not url:
        raise UsageError(f"no API URL: set ${URL_ENV} or pass --api-url")
    loopback = url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))
    if not (url.startswith("https://") or loopback):
        raise UsageError("the API URL must be https:// (plain http only for loopback)")
    return url


def build_request(
    args: argparse.Namespace, url: str, token: str
) -> tuple[str, str, dict[str, str], bytes | None]:
    """`(method, full URL, headers, body)` for the chosen command."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Requested-With": "waddles-admin",
    }
    if args.command == "status":
        return "GET", f"{url}{API_PATH}/", headers, None
    key = args.idempotency_key or str(uuid.uuid4())
    try:
        uuid.UUID(key)
    except ValueError as exc:
        raise UsageError("--idempotency-key must be a UUID") from exc
    payload: dict[str, int] = {"reasonCode": REASONS[args.reason]}
    if args.command == "retire":
        payload["version"] = args.version
    headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
    return (
        "POST",
        f"{url}{API_PATH}/{args.type}/{args.command}",
        headers,
        json.dumps(payload).encode(),
    )


def send(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    ca_bundle: str | None,
) -> tuple[int, dict[str, Any]]:
    """One HTTPS request; returns `(status, parsed JSON or {})`. Certificate checks stay on."""
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=context
        ) as resp:  # nosec B310
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def render(command: str, status: int, body: dict[str, Any]) -> str:
    """A short human summary (versions, states, error code) -- never the token or raw headers."""
    if status >= 400 or (command != "status" and not body.get("success", False)):
        err = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
        return f"refused: HTTP {status} {err.get('code', 'UNKNOWN')} - {err.get('message', '')}"
    if command == "status":
        lines = []
        for entry in body.get("types", []):
            versions = ", ".join(
                f"v{v['version']}:state{v['state']}" for v in entry["versions"]
            )
            lines.append(f"{entry['type']}: {versions}")
        return "\n".join(lines)
    if command == "rotate":
        return (
            f"rotated {body['type']}: v{body['previousVersion']} -> v{body['newVersion']} "
            f"(previous verifies until {body.get('previousVerifyUntil')})"
        )
    return f"retired {body['type']} v{body['version']} at {body['retiredAt']}"


def main(
    argv: Sequence[str] | None = None,
    environ: dict[str, str] | None = None,
    stdin: Any = None,
) -> int:
    """Run one command; returns the process exit code."""
    env = dict(os.environ) if environ is None else environ
    stream = sys.stdin if stdin is None else stdin
    try:
        args = build_parser().parse_args(argv)
        url = resolve_url(args, env)
        if args.command != "status" and not args.yes and not stream.isatty():
            raise UsageError("refusing to run a non-interactive change without --yes")
        if args.command != "status" and not args.yes:
            answer = input(
                f"{args.command} {args.type} ({args.reason}) -- type 'yes' to continue: "
            )
            if answer.strip().lower() != "yes":
                raise UsageError("aborted")
        token = read_token(args, env, stream)
        method, full_url, headers, body = build_request(args, url, token)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        status, parsed = send(method, full_url, headers, body, args.ca_bundle)
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        print(f"error: could not reach the API ({type(exc).__name__})", file=sys.stderr)
        return EXIT_NETWORK
    print(render(args.command, status, parsed))
    return EXIT_OK if status < 400 else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
