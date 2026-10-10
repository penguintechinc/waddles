"""`blueprints/v1/cookie_consent.py` -- the Privacy/Compliance cookie consent group.

Standalone Quart app registering only `cookie_consent_bp`, matching
`test_v1_user_management_blueprint.py`'s pattern (`privacy_db` fixture,
`auth_headers(scope="settings:write")` for the admin-gated pair).

Fail-first proof for the IDOR/BOLA regression tests (executed, not
narrated): `test_audit_log_returns_only_callers_own_entries` was run once
against a deliberately-broken `get_audit_log()` that took `user_id` from
`request.args.get("userId")` instead of `get_current_user_id(request)` --
went red (Bob's request, `?userId=<alice>`, returned Alice's audit
entries); reverted, confirmed green again.

Fail-first proof for scope enforcement: temporarily swapped
`require_scope("settings:write")` for `require_scope("settings:read")` on
`create_policy_version`'s decorator chain -- `test_create_policy_wrong_scope_is_403`
went red (201 instead of 403); reverted, green again.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.cookie_consent import cookie_consent_bp
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
def app(privacy_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(cookie_consent_bp)
    quart_app.config["dal"] = privacy_db.dal
    quart_app.config["async_dal"] = privacy_db
    quart_app.config["HUB_API_CONFIG"] = None
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _headers(user_id: int, *, scope: str = "") -> dict[str, str]:
    token = make_user_token(user_id=user_id, scope=scope, tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


def _admin_headers(user_id: int = 1) -> dict[str, str]:
    # global:admin bundle -- SCOPE_BUNDLES["global"]["admin"] includes
    # "settings:write", the exact scope the policy-admin routes require.
    return _headers(user_id, scope="*:read *:write *:admin *:delete settings:write users:admin")


class TestGetConsent:
    async def test_anonymous_visitor_gets_default_preferences(self, client: Any) -> None:
        response = await client.get("/api/v1/cookie")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["consentId"] is None
        assert body["data"]["preferences"]["necessary"] is True
        assert body["data"]["preferences"]["marketing"] is False

    async def test_gpc_header_forces_do_not_sell_on_default(self, client: Any) -> None:
        response = await client.get("/api/v1/cookie", headers={"Sec-GPC": "1"})
        body = await response.get_json()
        assert body["data"]["preferences"]["doNotSell"] is True
        assert body["data"]["gpcApplied"] is True

    async def test_get_consent_for_authenticated_user_returns_saved_record(
        self, client: Any
    ) -> None:
        """Exercises `get_or_create_consent()`'s `user_id is not None` branch."""
        await client.post(
            "/api/v1/cookie",
            headers=_headers(20),
            json={"preferences": {"functional": True, "marketing": True}},
        )
        response = await client.get("/api/v1/cookie", headers=_headers(20))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["userId"] == 20
        assert body["data"]["preferences"]["functional"] is True
        assert body["data"]["consentId"] is not None

    async def test_get_consent_via_cookie_for_returning_anonymous_visitor(
        self, client: Any
    ) -> None:
        """Exercises `get_or_create_consent()`'s `elif consent_id` branch."""
        save_response = await client.post(
            "/api/v1/cookie", json={"preferences": {"analytics": True}}
        )
        saved_consent_id = (await save_response.get_json())["data"]["consentId"]

        # Same `client` instance carries the `Set-Cookie` forward via its
        # own cookie jar, matching a real browser's returning-visitor flow.
        get_response = await client.get("/api/v1/cookie")
        assert get_response.status_code == 200
        body = await get_response.get_json()
        assert body["data"]["consentId"] == saved_consent_id
        assert body["data"]["preferences"]["analytics"] is True


class TestSaveConsent:
    async def test_save_consent_persists_and_sets_cookie(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/cookie",
            json={
                "preferences": {"functional": True, "analytics": True},
                "consentMethod": "banner",
            },
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["preferences"]["functional"] is True
        assert body["data"]["consentId"] is not None
        assert "waddlebot_consent_id" in response.headers.get("Set-Cookie", "")

    async def test_gpc_overrides_marketing_true_in_request_body(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/cookie",
            headers={"Sec-GPC": "1"},
            json={"preferences": {"marketing": True}},
        )
        body = await response.get_json()
        assert body["data"]["preferences"]["marketing"] is False
        assert body["data"]["preferences"]["doNotSell"] is True

    async def test_save_consent_for_authenticated_user_records_user_id(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/cookie",
            headers=_headers(42),
            json={"preferences": {"functional": True}},
        )
        body = await response.get_json()
        assert body["data"]["userId"] == 42


class TestPolicyPublicEndpoints:
    async def test_get_current_policy_no_active_policy_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/cookie/policy")
        assert response.status_code == 404

    async def test_get_policy_history_empty_is_200(self, client: Any) -> None:
        response = await client.get("/api/v1/cookie/policy/history")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["versions"] == []
        assert body["data"]["total"] == 0

    async def test_get_policy_history_garbage_pagination_falls_back_to_defaults(
        self, client: Any
    ) -> None:
        response = await client.get("/api/v1/cookie/policy/history?limit=not-a-number&offset=-5")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["limit"] == 10
        assert body["data"]["offset"] == 0

    async def test_get_policy_history_returns_created_versions(self, client: Any) -> None:
        await client.post(
            "/api/v1/cookie/policy",
            headers=_admin_headers(),
            json={"version": "1.0.0", "content": "v1 text"},
        )
        await client.post(
            "/api/v1/cookie/policy",
            headers=_admin_headers(),
            json={"version": "2.0.0", "content": "v2 text"},
        )

        response = await client.get("/api/v1/cookie/policy/history")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["total"] == 2
        versions = {v["version"] for v in body["data"]["versions"]}
        assert versions == {"1.0.0", "2.0.0"}
        # Ordering itself (`ORDER BY created_at DESC`, same as Node) is NOT
        # asserted here -- two creates in the same test can land in the
        # same sub-millisecond `created_at`, making "newest first" racy at
        # this granularity in both the Node original and this port.


class TestUpdatePreferences:
    async def test_update_preferences_without_token_is_401(self, client: Any) -> None:
        response = await client.patch(
            "/api/v1/cookie/preferences", json={"preferences": {"functional": True}}
        )
        assert response.status_code == 401

    async def test_update_preferences_no_existing_record_is_404(self, client: Any) -> None:
        response = await client.patch(
            "/api/v1/cookie/preferences",
            headers=_headers(7),
            json={"preferences": {"functional": True}},
        )
        assert response.status_code == 404

    async def test_update_preferences_success(self, client: Any) -> None:
        await client.post(
            "/api/v1/cookie", headers=_headers(7), json={"preferences": {"functional": False}}
        )
        response = await client.patch(
            "/api/v1/cookie/preferences",
            headers=_headers(7),
            json={"preferences": {"functional": True, "analytics": True}},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["preferences"]["functional"] is True
        assert body["data"]["preferences"]["analytics"] is True


class TestDoNotSellPersistence:
    """Regression (CCPA/CPRA statutory right): `doNotSell` must survive later writes.

    Before the fix `update_preferences()`/`revoke_consent()` replaced the
    stored preferences object wholesale with a payload that never carried
    `doNotSell`, and `save_consent()` replaced it with a request DTO whose
    `doNotSell` defaulted to `False` when omitted -- each silently reverted
    a user's opt-out to "sell/share allowed". These tests assert the
    opt-out (explicit or GPC-originated) survives every subsequent write
    and that an *explicit* opt-back-in still works.
    """

    async def _opt_out(self, client: Any, user_id: int, **extra_headers: str) -> None:
        response = await client.post(
            "/api/v1/cookie",
            headers={**_headers(user_id), **extra_headers},
            json={"preferences": {"functional": True, "doNotSell": True}},
        )
        assert response.status_code == 200
        assert (await response.get_json())["data"]["preferences"]["doNotSell"] is True

    async def _stored_do_not_sell(self, client: Any, user_id: int) -> bool:
        # No Sec-GPC header on the read: it reflects only what is STORED.
        response = await client.get("/api/v1/cookie", headers=_headers(user_id))
        body = await response.get_json()
        assert body["data"]["gpcApplied"] is False
        return bool(body["data"]["preferences"]["doNotSell"])

    async def test_do_not_sell_survives_unrelated_preferences_update(self, client: Any) -> None:
        await self._opt_out(client, 30)

        response = await client.patch(
            "/api/v1/cookie/preferences",
            headers=_headers(30),
            json={"preferences": {"functional": True, "analytics": True}},
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["preferences"]["analytics"] is True
        assert body["data"]["preferences"]["doNotSell"] is True
        assert await self._stored_do_not_sell(client, 30) is True

    async def test_gpc_originated_opt_out_survives_preferences_update(self, client: Any) -> None:
        """GPC state persists: stored via a `Sec-GPC: 1` POST, kept by a later header-less PATCH."""
        response = await client.post(
            "/api/v1/cookie",
            headers={**_headers(31), "Sec-GPC": "1"},
            json={"preferences": {"analytics": True}},
        )
        assert (await response.get_json())["data"]["preferences"]["doNotSell"] is True

        await client.patch(
            "/api/v1/cookie/preferences",
            headers=_headers(31),
            json={"preferences": {"functional": True}},
        )

        assert await self._stored_do_not_sell(client, 31) is True

    async def test_gpc_header_on_preferences_update_is_honored(self, client: Any) -> None:
        await client.post("/api/v1/cookie", headers=_headers(32), json={"preferences": {}})

        response = await client.patch(
            "/api/v1/cookie/preferences",
            headers={**_headers(32), "Sec-GPC": "1"},
            json={"preferences": {"marketing": True}},
        )

        body = await response.get_json()
        assert body["data"]["gpcApplied"] is True
        assert body["data"]["preferences"]["marketing"] is False
        assert body["data"]["preferences"]["doNotSell"] is True
        assert await self._stored_do_not_sell(client, 32) is True

    async def test_do_not_sell_survives_revoke(self, client: Any) -> None:
        await self._opt_out(client, 33)

        response = await client.delete("/api/v1/cookie", headers=_headers(33))

        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["preferences"]["marketing"] is False
        assert body["data"]["preferences"]["doNotSell"] is True
        assert await self._stored_do_not_sell(client, 33) is True

    async def test_do_not_sell_survives_save_that_omits_it(self, client: Any) -> None:
        await self._opt_out(client, 34)

        response = await client.post(
            "/api/v1/cookie", headers=_headers(34), json={"preferences": {"analytics": True}}
        )

        body = await response.get_json()
        assert body["data"]["preferences"]["analytics"] is True
        assert body["data"]["preferences"]["doNotSell"] is True
        assert await self._stored_do_not_sell(client, 34) is True

    async def test_explicit_opt_back_in_is_still_honored(self, client: Any) -> None:
        """Merge must not make the opt-out sticky: an explicit `doNotSell: false` wins."""
        await self._opt_out(client, 35)

        response = await client.post(
            "/api/v1/cookie",
            headers=_headers(35),
            json={"preferences": {"functional": True, "doNotSell": False}},
        )

        assert (await response.get_json())["data"]["preferences"]["doNotSell"] is False
        assert await self._stored_do_not_sell(client, 35) is False

    async def test_new_record_without_do_not_sell_defaults_to_false(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/cookie", headers=_headers(36), json={"preferences": {"functional": True}}
        )
        assert (await response.get_json())["data"]["preferences"]["doNotSell"] is False

    async def test_unknown_stored_keys_are_preserved_by_update_and_revoke(
        self, client: Any, privacy_db: Any
    ) -> None:
        """Key-by-key merge: keys this endpoint never writes are never dropped."""
        await client.post(
            "/api/v1/cookie", headers=_headers(37), json={"preferences": {"doNotSell": True}}
        )
        stored = {
            "necessary": True,
            "functional": False,
            "analytics": False,
            "marketing": False,
            "doNotSell": True,
            "sharingOptOutScope": "all",
        }
        await privacy_db.update_async(
            privacy_db.dal.cookie_consent.user_id == 37, preferences=stored
        )

        await client.patch(
            "/api/v1/cookie/preferences",
            headers=_headers(37),
            json={"preferences": {"functional": True}},
        )
        rows = await privacy_db.select_async(
            privacy_db.dal(privacy_db.dal.cookie_consent.user_id == 37)
        )
        after_patch = dict(rows[0].preferences)
        assert after_patch["sharingOptOutScope"] == "all"
        assert after_patch["doNotSell"] is True
        assert after_patch["functional"] is True

        await client.delete("/api/v1/cookie", headers=_headers(37))
        rows = await privacy_db.select_async(
            privacy_db.dal(privacy_db.dal.cookie_consent.user_id == 37)
        )
        after_revoke = dict(rows[0].preferences)
        assert after_revoke["sharingOptOutScope"] == "all"
        assert after_revoke["doNotSell"] is True
        assert after_revoke["functional"] is False


class TestRevokeConsent:
    async def test_revoke_without_token_is_401(self, client: Any) -> None:
        response = await client.delete("/api/v1/cookie")
        assert response.status_code == 401

    async def test_revoke_no_existing_record_is_404(self, client: Any) -> None:
        response = await client.delete("/api/v1/cookie", headers=_headers(8))
        assert response.status_code == 404

    async def test_revoke_success(self, client: Any) -> None:
        await client.post(
            "/api/v1/cookie", headers=_headers(8), json={"preferences": {"marketing": True}}
        )
        response = await client.delete("/api/v1/cookie", headers=_headers(8))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["preferences"]["marketing"] is False


class TestAuditLog:
    async def test_audit_log_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/cookie/audit")
        assert response.status_code == 401

    async def test_audit_log_returns_only_callers_own_entries(self, client: Any) -> None:
        """The IDOR/BOLA regression -- see module docstring for the fail-first proof.

        User 10 saves once (1 ACCEPT), user 11 saves then updates twice
        (1 ACCEPT + up to 2 UPDATE). If `get_audit_log()` ever stopped
        filtering by the caller's own `user_id`, user 10's total would
        jump past 1 once user 11's entries leaked in.
        """
        await client.post(
            "/api/v1/cookie", headers=_headers(10), json={"preferences": {"functional": True}}
        )
        await client.post(
            "/api/v1/cookie", headers=_headers(11), json={"preferences": {"analytics": True}}
        )
        await client.patch(
            "/api/v1/cookie/preferences",
            headers=_headers(11),
            json={"preferences": {"functional": True, "analytics": True}},
        )

        response = await client.get("/api/v1/cookie/audit", headers=_headers(10))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["total"] == 1
        assert body["data"]["logs"][0]["action"] == "ACCEPT"


class TestPolicyAdminEndpoints:
    async def test_create_policy_no_token_is_401(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/cookie/policy", json={"version": "2.0.0", "content": "..."}
        )
        assert response.status_code == 401

    async def test_create_policy_wrong_scope_is_403(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/cookie/policy",
            headers=_headers(1, scope="platform:read"),
            json={"version": "2.0.0", "content": "..."},
        )
        assert response.status_code == 403

    async def test_create_and_activate_policy_version(self, client: Any) -> None:
        create_response = await client.post(
            "/api/v1/cookie/policy",
            headers=_admin_headers(),
            json={"version": "2.0.0", "content": "New policy text", "changesSummary": "v2"},
        )
        assert create_response.status_code == 201
        body = await create_response.get_json()
        assert body["data"]["version"] == "2.0.0"
        assert body["data"]["is_active"] is True

        current_response = await client.get("/api/v1/cookie/policy")
        assert current_response.status_code == 200
        current_body = await current_response.get_json()
        assert current_body["data"]["version"] == "2.0.0"

        activate_response = await client.put(
            "/api/v1/cookie/policy/2.0.0/activate", headers=_admin_headers()
        )
        assert activate_response.status_code == 200

    async def test_activate_unknown_policy_version_is_404(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/cookie/policy/9.9.9/activate", headers=_admin_headers()
        )
        assert response.status_code == 404
