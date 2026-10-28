"""Flag-ON, real-path E2E for marketplace sources (Phase 1, Task 11 / design sections 4-6).

Runs against a LIVE deployment (flag `waddles.marketplace-sources` ON for the admin tenant,
`gitProxy.enabled`, `sourceAutoUpdate.enabled`, the fixture git server reachable only through the
git-proxy host allowlist). Nothing here is mocked: every assertion is an HTTP/kubectl observation
of the real system, with counts asserted (a zero denominator is a failure).

Run only via `make test-marketplace-e2e` (scripts/marketplace_test_gate.sh e2e): that gate fails the
run on ANY skip, on zero tests executed, and on missing prerequisites. A plain `pytest` run skips
the whole module with a reason. When MARKETPLACE_E2E_ENABLED=1 a missing prerequisite is a hard
FAILURE (fixture `env`), never a skip.

Environment (all secrets via ENV, never argv):
    MARKETPLACE_E2E_ENABLED=1
    MARKETPLACE_E2E_BASE_URL                 hub-api base, e.g. https://hub.example
    MARKETPLACE_E2E_ADMIN_TOKEN              platform:admin JWT (flag-on tenant, Enterprise tier)
    MARKETPLACE_E2E_PRO_ADMIN_TOKEN          platform:admin JWT of a PROFESSIONAL-tier tenant
    MARKETPLACE_E2E_FLAGOFF_ADMIN_TOKEN      platform:admin JWT of a tenant with the flag OFF
    MARKETPLACE_E2E_TENANT_TOKEN / _TENANT_SLUG   tenant:admin JWT + slug (make_available)
    MARKETPLACE_E2E_COMMUNITY_TOKEN / _COMMUNITY_ID   community-admin JWT + id (activate)
    MARKETPLACE_E2E_FIXTURE_URL              fixture admin API base (https://<fixture-host>)
    MARKETPLACE_E2E_FIXTURE_HOST             fixture clone base the PROXY dials (https://<host>)
    FIXTURE_ADMIN_TOKEN                      fixture admin bearer
    MARKETPLACE_E2E_KUBE_CONTEXT / _NAMESPACE   kubectl target (cronjob trigger, CNP probes, logs)
    MARKETPLACE_E2E_REBIND_HOST              DNS name that rebinds to a private IP after answer 1
    MARKETPLACE_E2E_EXECUTOR_LOG_MARKER      executor log substring proving signature-verified load
                                             (default "signature verified")
    MARKETPLACE_E2E_HUB_DEPLOY               hub-api workload for the CNP probe

Mutation procedure (plan Step 2; per gate, run at 11C): see scripts/marketplace_mutation_check.py.
Each gate's cluster test is selectable with `-k` (sha_pin / namespace / ack / tier / attestation).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess  # noqa: S404
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
ENABLED = os.environ.get("MARKETPLACE_E2E_ENABLED") == "1"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_cluster,
    pytest.mark.skipif(
        not ENABLED,
        reason="opt-in cluster E2E: run `make test-marketplace-e2e` (needs a deployment)",
    ),
]

REQUIRED = (
    "MARKETPLACE_E2E_BASE_URL", "MARKETPLACE_E2E_ADMIN_TOKEN", "MARKETPLACE_E2E_PRO_ADMIN_TOKEN",
    "MARKETPLACE_E2E_FLAGOFF_ADMIN_TOKEN", "MARKETPLACE_E2E_TENANT_TOKEN",
    "MARKETPLACE_E2E_TENANT_SLUG", "MARKETPLACE_E2E_COMMUNITY_TOKEN",
    "MARKETPLACE_E2E_COMMUNITY_ID", "MARKETPLACE_E2E_FIXTURE_URL", "MARKETPLACE_E2E_FIXTURE_HOST",
    "FIXTURE_ADMIN_TOKEN", "MARKETPLACE_E2E_KUBE_CONTEXT", "MARKETPLACE_E2E_NAMESPACE",
    "MARKETPLACE_E2E_REBIND_HOST",
)  # fmt: skip
SRC = "/api/v1/marketplace/sources"
POLL_S = 3.0
POLL_MAX_S = 180.0


@dataclass(slots=True, frozen=True)
class Env:
    """Validated E2E configuration (built once; secrets stay in this process)."""

    base: str
    admin: str
    pro_admin: str
    flagoff_admin: str
    tenant_token: str
    tenant_slug: str
    community_token: str
    community_id: int
    fixture_url: str
    fixture_host: str
    fixture_token: str
    kube_ctx: str
    namespace: str
    rebind_host: str
    executor_marker: str
    hub_deploy: str


@pytest.fixture(scope="session")
def env() -> Env:
    """Fail LOUDLY (never skip) when the opt-in run is missing any prerequisite."""
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        pytest.fail(
            f"marketplace E2E prerequisites missing: {', '.join(missing)}",
            pytrace=False,
        )
    g = os.environ.__getitem__
    return Env(
        base=g("MARKETPLACE_E2E_BASE_URL").rstrip("/"),
        admin=g("MARKETPLACE_E2E_ADMIN_TOKEN"),
        pro_admin=g("MARKETPLACE_E2E_PRO_ADMIN_TOKEN"),
        flagoff_admin=g("MARKETPLACE_E2E_FLAGOFF_ADMIN_TOKEN"),
        tenant_token=g("MARKETPLACE_E2E_TENANT_TOKEN"),
        tenant_slug=g("MARKETPLACE_E2E_TENANT_SLUG"),
        community_token=g("MARKETPLACE_E2E_COMMUNITY_TOKEN"),
        community_id=int(g("MARKETPLACE_E2E_COMMUNITY_ID")),
        fixture_url=g("MARKETPLACE_E2E_FIXTURE_URL").rstrip("/"),
        fixture_host=g("MARKETPLACE_E2E_FIXTURE_HOST").rstrip("/"),
        fixture_token=g("FIXTURE_ADMIN_TOKEN"),
        kube_ctx=g("MARKETPLACE_E2E_KUBE_CONTEXT"),
        namespace=g("MARKETPLACE_E2E_NAMESPACE"),
        rebind_host=g("MARKETPLACE_E2E_REBIND_HOST"),
        executor_marker=os.environ.get("MARKETPLACE_E2E_EXECUTOR_LOG_MARKER", "signature verified"),
        hub_deploy=os.environ.get("MARKETPLACE_E2E_HUB_DEPLOY", "deploy/waddlebot-hub-api"),
    )


@pytest.fixture(scope="session")
def http() -> Iterator[httpx.Client]:
    """One shared client (TLS verification ON -- the E2E must exercise real certificates)."""
    with httpx.Client(timeout=120.0) as client:
        yield client


@pytest.fixture(scope="session")
def ack_hash() -> str:
    """The current sandbox trust-warning hash, read from the single source of truth."""
    path = REPO / "hub_api" / "services" / "marketplace_sandbox_warning.py"
    spec = importlib.util.spec_from_file_location("mkt_sandbox_warning", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.WARNING_HASH)


class Api:
    """Thin authenticated wrapper over the hub-api + fixture admin API + kubectl."""

    def __init__(self, env: Env, http: httpx.Client) -> None:
        """Bind config and client."""
        self.env, self.http = env, http

    def call(
        self, method: str, path: str, token: str, body: object | None = None
    ) -> httpx.Response:
        """One hub-api request; the caller asserts status (no implicit raise_for_status)."""
        return self.http.request(
            method,
            self.env.base + path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    def admin(self, method: str, path: str, body: object | None = None) -> httpx.Response:
        """platform:admin call (Enterprise, flag-on tenant)."""
        return self.call(method, path, self.env.admin, body)

    def fixture(self, method: str, path: str, body: object | None = None) -> Any:
        """Fixture-server admin API call; non-200 is a hard failure."""
        r = self.http.request(
            method,
            self.env.fixture_url + "/_admin" + path,
            json=body,
            headers={"Authorization": f"Bearer {self.env.fixture_token}"},
        )
        assert r.status_code == 200, f"fixture {path}: {r.status_code} {r.text}"
        return r.json()

    def kubectl(self, *args: str, timeout: int = 240) -> str:
        """Kubectl against the E2E context/namespace; non-zero exit is a hard failure."""
        proc = subprocess.run(  # noqa: S603
            ["kubectl", "--context", self.env.kube_ctx, "-n", self.env.namespace, *args],  # noqa: S607
            capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
        assert proc.returncode == 0, f"kubectl {' '.join(args)}: {proc.stderr.strip()}"
        return proc.stdout

    def add_source(
        self,
        name: str,
        repo: str,
        mode: str = "prebuilt",
        *,
        token: str | None = None,
        **over: Any,
    ) -> httpx.Response:
        """POST a source pinned at the fixture repo's current head (publisher key for prebuilt)."""
        body: dict[str, Any] = {
            "name": f"{name}-{uuid.uuid4().hex[:6]}",
            "git_url": f"{self.env.fixture_host}/{repo}.git",
            "ref": "main",
            "build_mode": mode,
            "commit_sha": self.fixture("GET", "/repos")[repo]["head"],
        }
        if mode == "prebuilt":
            body["publisher_public_key"] = self.fixture("GET", "/publisher-key")["public_key"]
        body.update(over)
        return self.call("POST", SRC, token or self.env.admin, body)

    def source(self, public_id: str) -> dict[str, Any]:
        """GET one source DTO (admin)."""
        r = self.admin("GET", f"{SRC}/{public_id}")
        assert r.status_code == 200, r.text
        return dict(r.json()["source"])

    def trigger_autoupdate(self) -> None:
        """Run the auto-update CronJob NOW (a Job from it) and wait for success."""
        cj = self.kubectl(
            "get", "cronjob", "-l", "app.kubernetes.io/component=marketplace-source-rescan",
            "-o", "name",
        ).split()  # fmt: skip
        assert len(cj) == 1, f"expected exactly 1 auto-update cronjob, found {cj}"
        job = f"mkt-e2e-{uuid.uuid4().hex[:8]}"
        self.kubectl("create", "job", job, f"--from={cj[0]}")
        try:
            self.kubectl("wait", "--for=condition=complete", f"job/{job}", "--timeout=180s")
        finally:
            self.kubectl("delete", "job", job, "--ignore-not-found")

    def poll(self, what: str, fn: Callable[[], bool]) -> None:
        """Poll `fn` until true or fail loudly with `what` (bounded)."""
        deadline = time.monotonic() + POLL_MAX_S
        while time.monotonic() < deadline:
            if fn():
                return
            time.sleep(POLL_S)
        pytest.fail(f"timed out waiting for: {what}")

    def installs(self) -> list[dict[str, Any]]:
        """Current global installs (the platform catalog)."""
        r = self.admin("GET", "/api/v1/apps/installs")
        assert r.status_code == 200, r.text
        return list(r.json()["installs"])

    def install_for(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        """Global installs whose app_id lives under this source's namespace."""
        prefix = _prefix(source)
        return [i for i in self.installs() if i["appId"].startswith(prefix)]


def _prefix(source: dict[str, Any]) -> str:
    return f"waddles.integrations.src-{uuid.UUID(source['publicId']).hex[:12]}."


@pytest.fixture(scope="session")
def api(env: Env, http: httpx.Client) -> Api:
    """The shared API driver."""
    return Api(env, http)


def _publish_signed_and_pin(api: Api, repo: str, version: str, source: dict[str, Any]) -> str:
    """Publisher flow: sign for THIS source, push, let auto-update detect it, approve the pin."""
    sha = api.fixture(
        "POST",
        f"/repos/{repo}/commit",
        {"version": version, "sign_for": source["publicId"]},
    )["head"]
    api.trigger_autoupdate()
    pending = api.source(source["publicId"])
    assert pending["pendingCommitSha"] == sha and pending["status"] == "needs_approval", pending
    r = api.admin(
        "POST",
        f"{SRC}/{source['publicId']}/approve-update",
        {"pending_commit_sha": sha},
    )
    assert r.status_code == 200, r.text
    assert r.json()["source"]["pinnedCommitSha"] == sha
    return sha


def _approve_all(api: Api, source: dict[str, Any], version: str) -> str:
    """Rescan -> approve the (single) ingested version through the GENERIC route. Returns app_id."""
    r = api.admin("POST", f"{SRC}/{source['publicId']}/rescan")
    assert r.status_code == 200, r.text
    s = r.json()
    counts = (s["examined"], s["accepted"], s["rejectedAtScan"], s["created"])
    assert (*counts, s["rejectedAtIngest"]) == (1, 1, 0, 1, 0), s
    app_id = f"{_prefix(source)}hello"
    assert api.install_for(source) == [], "ingested versions must land UNAPPROVED, not installed"
    perm = api.admin("GET", f"/api/v1/apps/{app_id}/versions/{version}/permissions")
    assert perm.status_code == 200, perm.text
    ok = api.admin(
        "POST",
        f"/api/v1/apps/{app_id}/versions/{version}/approve",
        {"permissionHash": perm.json()["permissionHash"]},
    )
    assert ok.status_code == 200, ok.text
    installed = api.install_for(source)
    assert [(i["appId"], i["version"], i["installSource"]) for i in installed] == [
        (app_id, version, "human")
    ]
    return app_id


def _activate(api: Api, app_id: str) -> None:
    """Tenant make_available -> community activate (both must be 201)."""
    e = api.env
    r = api.call(
        "POST", f"/api/v1/marketplace/tenant/{e.tenant_slug}/bundles", e.tenant_token,
        {"appId": app_id},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    r = api.call(
        "POST", f"/api/v1/marketplace/community/{e.community_id}/bundles", e.community_token,
        {"appId": app_id},
    )  # fmt: skip
    assert r.status_code == 201, r.text


def _executor_ran(api: Api, app_id: str) -> None:
    """The real executor loaded `app_id` and verified its PLATFORM signature (log evidence)."""

    def seen() -> bool:
        logs = api.kubectl(
            "logs", "-l", "app.kubernetes.io/component=bundle-executor-action", "--tail=2000",
            "--all-containers",
        )  # fmt: skip
        return any(app_id in ln and api.env.executor_marker in ln for ln in logs.splitlines())

    api.poll(f"executor log line with {app_id!r} + {api.env.executor_marker!r}", seen)


def test_flag_on_prebuilt_source_full_lifecycle(api: Api) -> None:
    """Add prebuilt -> proxy scan -> approve -> platform re-sign -> install -> activate -> RUNS."""
    r = api.add_source("prebuilt", "prebuilt")
    assert r.status_code == 201, r.text
    source = r.json()["source"]
    assert source["isBuiltin"] is False and source["buildMode"] == "prebuilt"
    assert source["publisherKeyFingerprint"]
    _publish_signed_and_pin(api, "prebuilt", "1.0.1", source)
    app_id = _approve_all(api, source, "1.0.1")
    _activate(api, app_id)
    _executor_ran(api, app_id)


def test_flag_on_sandbox_source_real_job_build(api: Api, env: Env, ack_hash: str) -> None:
    """Enterprise sandbox source -> real in-cluster Job build -> approve -> install -> execute.

    KNOWN GAP: nothing calls `marketplace_sandbox_builder.build_and_ingest` and rescan refuses
    non-prebuilt sources, so a sandbox build cannot be triggered over the API yet. This test is
    deliberately NOT skipped: it stays RED until the trigger exists (never a silent pass).
    """
    r = api.add_source("sandbox", "sandbox", "sandbox", sandbox_trust_ack=ack_hash)
    assert r.status_code == 201, r.text
    source = r.json()["source"]
    assert source["sandboxAcknowledged"] is True
    built = api.admin("POST", f"{SRC}/{source['publicId']}/rescan")
    assert built.status_code == 200, (
        f"sandbox build not triggerable: {built.status_code} {built.text}"
    )
    assert built.json()["created"] == 1
    app_id = _approve_all_sandbox(api, source)
    _activate(api, app_id)
    _executor_ran(api, app_id)


def _approve_all_sandbox(api: Api, source: dict[str, Any]) -> str:
    app_id = f"{_prefix(source)}hello"
    perm = api.admin("GET", f"/api/v1/apps/{app_id}/versions/1.0.0/permissions")
    assert perm.status_code == 200, perm.text
    ok = api.admin(
        "POST", f"/api/v1/apps/{app_id}/versions/1.0.0/approve",
        {"permissionHash": perm.json()["permissionHash"]},
    )  # fmt: skip
    assert ok.status_code == 200, ok.text
    assert len(api.install_for(source)) == 1
    return app_id


def test_flag_on_sandbox_build_job_has_no_egress(api: Api, env: Env, ack_hash: str) -> None:
    """Negative fixture: a build that tries egress must FAIL (CNP default-deny), never connect."""
    r = api.add_source("sbx-egress", "sandbox-egress", "sandbox", sandbox_trust_ack=ack_hash)
    assert r.status_code == 201, r.text
    source = r.json()["source"]
    built = api.admin("POST", f"{SRC}/{source['publicId']}/rescan")
    assert built.status_code in (422, 502, 409), (
        f"egress-probing build must be refused: {built.text}"
    )
    assert api.source(source["publicId"])["status"] == "error"
    assert api.install_for(source) == []


def test_auto_update_is_pending_not_installed_then_approved(api: Api) -> None:
    """New upstream commit -> PENDING, NOT installed, admin notified; approve-update moves pin."""
    r = api.add_source("autoupdate", "autoupdate")
    assert r.status_code == 201, r.text
    source = r.json()["source"]
    first = _publish_signed_and_pin(api, "autoupdate", "1.0.1", source)
    app_id = _approve_all(api, source, "1.0.1")
    before = api.install_for(source)
    new = api.fixture(
        "POST",
        "/repos/autoupdate/commit",
        {"version": "1.1.0", "sign_for": source["publicId"]},
    )["head"]
    api.trigger_autoupdate()
    after = api.source(source["publicId"])
    assert after["pendingCommitSha"] == new and after["pinnedCommitSha"] == first, after
    assert api.install_for(source) == before, "an update must NEVER install without approval"
    audit = api.admin(
        "GET",
        "/api/v1/platform/audit-log?action=marketplace_source_update_available&limit=100",
    )
    assert audit.status_code == 200, audit.text
    mine = [e for e in audit.json()["entries"] if e["targetId"] == source["publicId"]]
    assert len(mine) >= 2, "admin notification (audit chain) missing for the pending updates"
    ok = api.admin(
        "POST",
        f"{SRC}/{source['publicId']}/approve-update",
        {"pending_commit_sha": new},
    )
    assert ok.status_code == 200 and ok.json()["source"]["pinnedCommitSha"] == new, ok.text
    assert api.install_for(source) == before, "advancing the pin alone must not install"
    assert app_id


def test_force_push_behind_pin_is_error_and_pinned_bundles_untouched(api: Api) -> None:
    """HISTORY_REWRITTEN: a force-push that drops the pinned commit -> ERROR; installs untouched."""
    r = api.add_source("rewrite", "rewrite")
    assert r.status_code == 201, r.text
    source = r.json()["source"]
    _publish_signed_and_pin(api, "rewrite", "1.0.1", source)
    _approve_all(api, source, "1.0.1")
    before = api.install_for(source)
    pinned = api.source(source["publicId"])["pinnedCommitSha"]
    api.fixture("POST", "/repos/rewrite/force-push", {})
    api.trigger_autoupdate()
    after = api.source(source["publicId"])
    assert after["status"] == "error", f"force-push must set ERROR, got {after['status']}"
    assert after["pinnedCommitSha"] == pinned
    assert api.install_for(source) == before


HOSTILE = (
    ("hostile-symlink-escape", "symlink_escape"),
    ("hostile-symlink-abs", "symlink_absolute"),
    ("hostile-zip-bomb", "archive_bomb"),
    ("hostile-deep-path", "too_deep"),
    ("hostile-core-shadow", None),
    ("hostile-other-prefix", None),
    ("hostile-denylisted-dep", None),
)


@pytest.mark.parametrize(("repo", "reason"), HOSTILE, ids=[h[0] for h in HOSTILE])
def test_hostile_fixture_rejected_end_to_end(api: Api, repo: str, reason: str | None) -> None:
    """Every hostile repo is rejected through the REAL proxy+scanner; nothing is ingested."""
    r = api.add_source(repo, repo)
    assert r.status_code == 201, r.text
    source = r.json()["source"]
    scan = api.admin("POST", f"{SRC}/{source['publicId']}/rescan")
    if reason is not None:  # whole-source guard trip
        assert scan.status_code == 422 and reason in scan.text, scan.text
    else:  # per-bundle rejection: the only bundle is refused, none created
        assert scan.status_code == 200, scan.text
        s = scan.json()
        assert (s["examined"], s["accepted"], s["created"]) == (1, 0, 0), s
        assert s["rejectedAtScan"] == 1, s
    assert api.install_for(source) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://git.example.org/r.git",
        "https://127.0.0.1/r.git",
        "https://169.254.169.254/r.git",
        "https://10.0.0.5/r.git",
        "file:///etc/passwd",
        "ssh://git@git.example.org/r.git",
    ],
)
def test_ssrf_and_non_https_rejected_at_the_api(api: Api, url: str) -> None:
    """SSRF host / non-https URL refused at add -- no row is created."""
    before = api.admin("GET", SRC).json()["sources"]
    r = api.add_source("ssrf", "prebuilt", git_url=url)
    assert r.status_code == 400, r.text
    assert len(api.admin("GET", SRC).json()["sources"]) == len(before)


def test_dns_rebind_host_cannot_reach_a_private_address(api: Api, env: Env) -> None:
    """A host that rebinds to a private IP is denied by the PROXY's own resolve+check."""
    r = api.add_source("rebind", "prebuilt", git_url=f"https://{env.rebind_host}/prebuilt.git")
    if r.status_code != 201:  # rejected at add (first answer already private) is also a denial
        assert r.status_code == 400, r.text
        return
    source = r.json()["source"]
    scan = api.admin("POST", f"{SRC}/{source['publicId']}/rescan")
    assert scan.status_code == 422, f"rebind host must be refused: {scan.status_code} {scan.text}"
    assert api.install_for(source) == []


def test_direct_git_egress_is_denied_by_the_cnp(api: Api, env: Env) -> None:
    """hub-api cannot reach the fixture host directly: only the git-proxy path exists."""
    host = env.fixture_host.removeprefix("https://")
    probe = (
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(5)\n"
        f"try:\n s.connect(({host!r},443)); print('CONNECTED'); sys.exit(1)\n"
        "except OSError as e:\n print('DENIED', type(e).__name__)\n"
    )
    out = api.kubectl("exec", env.hub_deploy, "--", "python3", "-c", probe)
    assert "DENIED" in out and "CONNECTED" not in out, out


def test_flag_off_is_404(api: Api, env: Env) -> None:
    """Flag OFF for the tenant: the capability does not exist (404), for every verb."""
    for method, path in (("GET", SRC), ("POST", SRC), ("GET", f"{SRC}/{uuid.uuid4()}")):
        r = api.call(method, path, env.flagoff_admin, {} if method == "POST" else None)
        assert r.status_code == 404, f"{method} {path}: {r.status_code}"


def test_below_tier_sandbox_is_402_even_with_a_valid_ack(api: Api, env: Env, ack_hash: str) -> None:
    """Professional tenant may add prebuilt but a SANDBOX source is blocked with an upgrade path."""
    r = api.add_source(
        "tier", "sandbox", "sandbox", token=env.pro_admin, sandbox_trust_ack=ack_hash
    )
    assert r.status_code == 402, r.text
    assert json.loads(r.text).get("requiredTier") or "enterprise" in r.text.lower()


def test_builtin_source_present_and_core_shadow_rejected(api: Api) -> None:
    """Exactly one built-in source exists, is immutable via the API, and core.* is unclaimable."""
    sources = api.admin("GET", SRC).json()["sources"]
    builtin = [s for s in sources if s["isBuiltin"]]
    assert len(builtin) == 1, f"expected exactly 1 builtin source, got {len(builtin)}"
    assert builtin[0]["gitUrl"] is None
    assert api.admin("DELETE", f"{SRC}/{builtin[0]['publicId']}").status_code in (
        400,
        403,
        404,
        409,
    )
    shadow = api.add_source("shadow", "prebuilt", is_builtin=True)
    assert shadow.status_code == 400, "is_builtin must not be settable through the API"
    # a hostile repo claiming waddles.core.* is refused (covered per-bundle above); the installed
    # core catalog is unchanged by every test in this module:
    assert not [
        i
        for i in api.installs()
        if i["appId"].startswith("waddles.core.") and i["installSource"] == "human"
    ]
