"""Headlessly provision the in-cluster alpha PostHog and seed command flags.

Stdlib-only (urllib) port of license-server's scripts/posthog/provision-posthog.py,
trimmed to what waddlebot's alpha-only in-cluster PostHog needs: no `httpx` /
`pip install` at runtime, so the Job never depends on PyPI reachability from a
network-locked-down alpha pod. Runs as a Helm post-install/post-upgrade hook
(templates/infrastructure/posthog/bootstrap.yaml) once posthog-web is healthy.

Idempotent: if the target Secret already has POSTHOG_API_KEY, this exits
immediately (action="skip") -- a personal API key's secret value is only
returned at creation time, so re-running would mint a second, orphaned key
otherwise. Safe to run on every `helm upgrade` for that reason.

Steps:
  1. Wait for posthog-web's /_health to return 200.
  2. Sign up the first admin user (or log in if already provisioned).
  3. Resolve the current organization + project.
  4. Mint a personal API key (feature_flag read+write scope).
  5. Seed the WADDLES_SEED_FLAGS (comma-separated keys) as ENABLED,
     100% rollout -- alpha-only explicit enablement so the 4 command
     flags are immediately testable (critical-rules.md's "default OFF"
     rule is a product-code default, not a constraint on this one-time
     alpha seed).
  6. PATCH (create if missing) the K8s Secret named by BOOTSTRAP_SECRET_NAME
     with POSTHOG_API_KEY, then annotate BOOTSTRAP_RESTART_DEPLOYMENT to
     roll so it picks up the new env var.

Env vars: see the `_env(...)` calls in `main()`.
Exit 0 (JSON on stdout) on success; non-zero with a message on stderr otherwise.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
K8S_API_BASE = "https://kubernetes.default.svc"


def _env(name: str, default: str = "", required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"error: {name} is required", file=sys.stderr)
        sys.exit(2)
    return val


def _http(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: dict | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict, dict]:
    """Minimal urllib JSON request helper. Returns (status, response_headers, json_body)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = json.loads(raw) if raw else {}
            return resp.status, dict(resp.headers.items()), parsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"detail": raw.decode("utf-8", "replace")[:300]}
        return e.code, dict(e.headers.items()) if e.headers else {}, parsed


def _wait_for_posthog(base: str, attempts: int, delay_s: float) -> None:
    for attempt in range(1, attempts + 1):
        try:
            status, _, _ = _http(f"{base}/_health", timeout=5)
            if status == 200:
                return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if attempt < attempts:
            time.sleep(delay_s)
    print(f"error: PostHog health check failed after {attempts} attempts", file=sys.stderr)
    sys.exit(1)


def _csrf_headers(base: str) -> tuple[dict[str, str], str | None]:
    """Prime a CSRF cookie via GET /login and return (headers, cookie_header)."""
    req = urllib.request.Request(f"{base}/login", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie_header = resp.headers.get("Set-Cookie", "")
    token = None
    for part in cookie_header.split(","):
        for kv in part.split(";"):
            kv = kv.strip()
            if kv.startswith(("posthog_csrftoken=", "csrftoken=")):
                token = kv.split("=", 1)[1]
    headers = {"Referer": base + "/", "Origin": base}
    if token:
        headers["X-CSRFToken"] = token
    cookie_value = None
    if "posthog_csrftoken" in cookie_header or "csrftoken" in cookie_header:
        cookie_value = cookie_header.split(";")[0]
    if cookie_value:
        headers["Cookie"] = cookie_value
    return headers, token


def _k8s_request(path: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    try:
        with open(SA_TOKEN_PATH) as f:
            token = f.read().strip()
    except FileNotFoundError:
        print("error: not running in-cluster (no serviceaccount token)", file=sys.stderr)
        sys.exit(1)
    ctx = ssl.create_default_context(cafile=SA_CA_PATH)
    headers = {"Authorization": f"Bearer {token}"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{K8S_API_BASE}{path}", data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if method == "PATCH":
        req.add_header("Content-Type", "application/strategic-merge-patch+json")
    elif data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return e.code, {"detail": raw.decode("utf-8", "replace")[:300]}


def _namespace() -> str:
    ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    try:
        with open(ns_path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "default"


def main() -> int:
    base = _env("POSTHOG_URL", "http://posthog-web:8000").rstrip("/")
    email = _env("POSTHOG_BOOTSTRAP_EMAIL", "alpha-admin@waddlebot.localhost.local")
    org = _env("POSTHOG_BOOTSTRAP_ORG", "Waddlebot Alpha")
    label = _env("POSTHOG_KEY_LABEL", "waddlebot-alpha-flags")
    secret_name = _env("BOOTSTRAP_SECRET_NAME", required=True)
    restart_deployment = _env("BOOTSTRAP_RESTART_DEPLOYMENT", "")
    wait_attempts = int(_env("POSTHOG_WAIT_ATTEMPTS", "60"))
    wait_delay = float(_env("POSTHOG_WAIT_DELAY_S", "5.0"))
    seed_flags = [f.strip() for f in _env("WADDLES_SEED_FLAGS", "").split(",") if f.strip()]
    namespace = _namespace()

    # Idempotent skip: don't mint a second orphaned key on repeat `helm upgrade`.
    status, existing = _k8s_request(f"/api/v1/namespaces/{namespace}/secrets/{secret_name}")
    if status == 200:
        data = existing.get("data", {})
        if data.get("POSTHOG_API_KEY"):
            print(json.dumps({"action": "skip", "reason": "key already provisioned"}))
            return 0

    # Password: generate once, store alongside the API key so re-runs (idempotent
    # skip above) never need it again. Never logged.
    import secrets as pysecrets

    password = pysecrets.token_urlsafe(24)

    _wait_for_posthog(base, wait_attempts, wait_delay)

    headers, _ = _csrf_headers(base)
    login_status, _, _ = _http(
        f"{base}/api/login/", method="POST", headers=headers, body={"email": email, "password": password}
    )
    if login_status == 200:
        action = "login"
    else:
        headers, _ = _csrf_headers(base)
        signup_status, _, signup_body = _http(
            f"{base}/api/signup/",
            method="POST",
            headers=headers,
            body={
                "first_name": "Alpha",
                "email": email,
                "password": password,
                "organization_name": org,
                "role_at_organization": "engineering",
            },
        )
        if signup_status not in (200, 201):
            print(f"error: signup failed {signup_status}: {signup_body}", file=sys.stderr)
            return 1
        action = "signup"

    headers, _ = _csrf_headers(base)

    org_status, _, org_body = _http(f"{base}/api/organizations/@current/", headers=headers)
    if org_status != 200:
        print(f"error: could not resolve organization ({org_status})", file=sys.stderr)
        return 1

    proj_status, _, proj_body = _http(f"{base}/api/organizations/@current/projects/", headers=headers)
    project_id = None
    if proj_status == 200:
        results = proj_body.get("results", [])
        if results:
            project_id = results[0].get("id")

    key_status, _, key_body = _http(
        f"{base}/api/personal_api_keys/",
        method="POST",
        headers=headers,
        body={
            "label": label,
            "scopes": ["feature_flag:read", "feature_flag:write"],
            "scoped_organizations": [],
            "scoped_teams": [],
        },
    )
    if key_status not in (200, 201):
        print(f"error: personal API key creation failed {key_status}: {key_body}", file=sys.stderr)
        return 1
    api_key = key_body.get("value")

    # Seed the command flags ENABLED, 100% rollout -- alpha-only explicit
    # enablement (see this file's module docstring) so eightball/roll/lurk/count
    # are immediately testable once a caller actually gates on these keys.
    seeded = []
    if project_id:
        for flag_key in seed_flags:
            fstatus, _, _fbody = _http(
                f"{base}/api/projects/{project_id}/feature_flags/",
                method="POST",
                headers={**headers, "Authorization": f"Bearer {api_key}"},
                body={
                    "key": flag_key,
                    "name": f"waddles alpha seed: {flag_key}",
                    "active": True,
                    "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
                },
            )
            seeded.append({"flag": flag_key, "status": fstatus})

    string_data = {
        "POSTHOG_API_KEY": api_key,
        "POSTHOG_KEY": api_key,  # Rust penguin_licensing::LicenseConfig::from_env reads POSTHOG_KEY
        "POSTHOG_BOOTSTRAP_PASSWORD": password,
    }
    # base64 not required for stringData; the K8s API accepts stringData directly
    # on PATCH/CREATE with strategic-merge-patch content-type.
    if status == 200:
        patch_status, patch_body = _k8s_request(
            f"/api/v1/namespaces/{namespace}/secrets/{secret_name}",
            method="PATCH",
            body={"stringData": string_data},
        )
    else:
        patch_status, patch_body = _k8s_request(
            f"/api/v1/namespaces/{namespace}/secrets",
            method="POST",
            body={
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": secret_name, "namespace": namespace},
                "stringData": string_data,
            },
        )
    if patch_status not in (200, 201):
        print(f"error: secret write failed {patch_status}: {patch_body}", file=sys.stderr)
        return 1

    if restart_deployment:
        _k8s_request(
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{restart_deployment}",
            method="PATCH",
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"posthog-bootstrap/restarted-at": str(time.time_ns())}
                        }
                    }
                }
            },
        )

    print(
        json.dumps(
            {
                "action": action,
                "organization_id": org_body.get("id"),
                "project_id": project_id,
                "seeded_flags": seeded,
                "secret_patched": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
