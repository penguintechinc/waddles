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
FIRST_PARTY_VENDOR = os.environ.get("SMOKE_FIRST_PARTY_VENDOR", "penguintechinc")
# ASSUMED vendor-listing shapes (feature/marketplace-vendor-listing not yet published);
# adjust these constants if the real API differs.
VENDORS_PATH = "/api/v1/marketplace/vendors"
VENDORS_LIST_KEY = "vendors"
VENDOR_KEY_FIELD = "key"
VENDOR_NAME_FIELD = "name"
VENDOR_COUNT_FIELD = "count"
CATALOG_VENDOR_PARAM = "vendor"
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


def check_vendors(token):
    """Validate vendor listing shape, count sum == catalog total, first-party filter."""
    code, text = http("GET", API + VENDORS_PATH, token=token)
    data = jsonish(text)
    items = data.get(VENDORS_LIST_KEY) if isinstance(data, dict) else None
    if code != 200 or not isinstance(items, list) or not items:
        emit("marketplace-vendors", "FAIL", f"GET {VENDORS_PATH} -> {code} empty-or-missing")
        emit("marketplace-vendor-filter", "FAIL", "not run: no vendors")
        return
    bad = [
        i
        for i in items
        if not (
            isinstance(i, dict)
            and i.get(VENDOR_KEY_FIELD)
            and i.get(VENDOR_NAME_FIELD)
            and isinstance(i.get(VENDOR_COUNT_FIELD), int)
        )
    ]
    _, ct = http("GET", API + "/api/v1/marketplace/catalog?limit=1", token=token)
    cd = jsonish(ct)
    ctotal = total_of(cd) if isinstance(cd, dict) else None
    vsum = sum(
        i[VENDOR_COUNT_FIELD]
        for i in items
        if isinstance(i, dict) and isinstance(i.get(VENDOR_COUNT_FIELD), int)
    )
    first = next(
        (i for i in items if isinstance(i, dict) and i.get(VENDOR_KEY_FIELD) == FIRST_PARTY_VENDOR),
        None,
    )
    ok = not bad and ctotal == vsum and first is not None
    emit(
        "marketplace-vendors",
        "PASS" if ok else "FAIL",
        f"vendors={len(items)} malformed={len(bad)} sum={vsum} catalog_total={ctotal} "
        f"first_party={'found' if first else 'missing'}",
    )
    if first is None:
        emit("marketplace-vendor-filter", "FAIL", f"first-party vendor {FIRST_PARTY_VENDOR} absent")
        return
    vkey = first[VENDOR_KEY_FIELD]
    fc, ft = http(
        "GET", f"{API}/api/v1/marketplace/catalog?{CATALOG_VENDOR_PARAM}={vkey}&limit=1", token=token
    )
    fd = jsonish(ft)
    ftotal = total_of(fd) if isinstance(fd, dict) else None
    emit(
        "marketplace-vendor-filter",
        "PASS" if fc == 200 and (ftotal or 0) > 0 else "FAIL",
        f"catalog?{CATALOG_VENDOR_PARAM}={vkey} -> {fc} total={ftotal}",
    )


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
        for n in ("auth/me", "bundle-versions", "marketplace-catalog", "marketplace-vendors", "marketplace-vendor-filter", "communities"):
            emit(n, "FAIL", "not run: no token")
    else:
        check_authed(
            "auth/me",
            "/api/v1/auth/me",
            token,
            lambda d: (bool(d.get("user")) and d["user"].get("isSuperAdmin") is True, "isSuperAdmin"),
        )

        bv_code, bv_text = http("GET", API + "/api/v1/admin/bundle-versions?limit=5", token=token)
        bv = jsonish(bv_text)
        bv_total = total_of(bv) if isinstance(bv, dict) else None
        check_authed(
            "bundle-versions",
            "/api/v1/admin/bundle-versions?limit=5",
            token,
            lambda d: (isinstance(d.get("versions"), list), f"total={total_of(d)}"),
        )

        def catalog(d):
            total = d.get("pagination", {}).get("total")
            if not isinstance(bv_total, int) or not isinstance(total, int):
                return False, f"total={total} bundle_versions_total={bv_total}"
            return (
                total >= bv_total and bool(d.get("modules")),
                f"catalog_total={total} >= bundle_versions_total={bv_total}",
            )

        check_authed("marketplace-catalog", "/api/v1/marketplace/catalog?limit=5", token, catalog)
        check_vendors(token)
        check_authed(
            "communities",
            "/api/v1/superadmin/communities?limit=5",
            token,
            lambda d: (isinstance(d.get("communities"), list), f"total={total_of(d)}"),
        )

    emit(
        "bundle-activation-roundtrip",
        "SKIP",
        "activation POST/DELETE mutate community state; no read-only path",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
