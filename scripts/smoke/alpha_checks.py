"""In-cluster HTTP checks for the alpha smoke test.

Runs inside the short-lived smoke pod (stdlib only). Emits one line per check:
``RESULT|<name>|PASS|FAIL|SKIP|<detail>`` for scripts/smoke/alpha-smoke.sh to tally.
Credentials arrive via env (secretKeyRef) and are never printed.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = os.environ.get("SMOKE_API", "http://waddlebot-hub-api-v3:8204")
WEBUI = os.environ.get("SMOKE_WEBUI", "http://waddlebot-hub-webui-v3:8080")
MIN_CATALOG = int(os.environ.get("SMOKE_MIN_CATALOG", "100"))
TIMEOUT = 20


def emit(name: str, status: str, detail: str = "") -> None:
    """Print one machine-readable result line, with newlines/pipes stripped."""
    clean = re.sub(r"[|\r\n]+", " ", detail)[:200]
    print(f"RESULT|{name}|{status}|{clean}", flush=True)


def http(method, url, body=None, token=None):
    """Return (status, raw_text); HTTP errors yield their status, network errors (0, name)."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - any network failure is a failed check
        return 0, type(exc).__name__


def jsonish(text):
    """Parse JSON or return None."""
    try:
        return json.loads(text)
    except ValueError:
        return None


def check_authed(name, path, token, validate):
    """GET an authed endpoint; require 200 and validate(json) -> (ok, detail)."""
    code, text = http("GET", API + path, token=token)
    data = jsonish(text)
    if code != 200 or not isinstance(data, dict):
        emit(name, "FAIL", f"GET {path} -> {code}")
        return
    ok, detail = validate(data)
    emit(name, "PASS" if ok else "FAIL", f"GET {path} 200 {detail}")


def total_of(data):
    """Return pagination.total from a response dict."""
    return data.get("pagination", {}).get("total")


def main() -> int:
    """Run all HTTP checks."""
    for path in ("/health", "/ready"):
        code, _ = http("GET", API + path)
        emit(f"hub-api{path}", "PASS" if code == 200 else "FAIL", f"-> {code}")

    code, text = http("GET", WEBUI + "/")
    root = bool(re.search(r"id=[\"']root[\"']", text))
    emit("hub-webui/", "PASS" if code == 200 and root else "FAIL", f"-> {code} root_div={root}")

    code, _ = http("GET", API + "/api/v1/superadmin/communities")
    emit(
        "negative-unauth-protected",
        "PASS" if code in (401, 403) else "FAIL",
        f"unauthenticated superadmin/communities -> {code}",
    )

    email = os.environ.get("ADMIN_EMAIL", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    token = None
    code = 0
    if email and password:
        code, text = http(
            "POST", API + "/api/v1/auth/login", {"email": email, "password": password}
        )
        data = jsonish(text)
        token = data.get("token") if isinstance(data, dict) else None
    ok = bool(token) and token.count(".") == 2
    emit("admin-login", "PASS" if ok else "FAIL", f"-> {code if email else 'no creds'} jwt={ok}")

    if not ok:
        for n in ("auth/me", "marketplace-catalog", "communities", "bundle-versions"):
            emit(n, "FAIL", "not run: no token")
    else:
        check_authed(
            "auth/me",
            "/api/v1/auth/me",
            token,
            lambda d: (bool(d.get("user")) and d["user"].get("isSuperAdmin") is True, "isSuperAdmin"),
        )

        def catalog(d):
            total = total_of(d) or -1
            return total >= MIN_CATALOG and bool(d.get("modules")), f"total={total} min={MIN_CATALOG}"

        check_authed("marketplace-catalog", "/api/v1/marketplace/catalog?limit=5", token, catalog)
        check_authed(
            "communities",
            "/api/v1/superadmin/communities?limit=5",
            token,
            lambda d: (isinstance(d.get("communities"), list), f"total={total_of(d)}"),
        )
        check_authed(
            "bundle-versions",
            "/api/v1/admin/bundle-versions?limit=5",
            token,
            lambda d: (isinstance(d.get("versions"), list), f"total={total_of(d)}"),
        )

    emit(
        "bundle-activation-roundtrip",
        "SKIP",
        "activation POST/DELETE mutate community state; no read-only path",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
