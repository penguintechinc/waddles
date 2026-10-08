"""Tests for `services.bundle_instance_policy_service` -- the instance-wide policy layer.

Spec: instance policy sits ABOVE the 3 consent tiers -- a GLOBAL admin can
allow/deny a permission TYPE instance-wide, applying to every bundle
regardless of per-app approval. `net.http.private-ip` is deny-by-default.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from services import bundle_instance_policy_service as svc
from services import bundle_permission_service as perm_svc
from services.bundle_manifest_v2 import parse_bundle_manifest_v2

_APP_ID = "waddles.socials.music.default"
_VERSION = "3.0.0"


class _FakeValkeyClient:
    """Records every `xadd()` call -- no real Valkey connection in a unit test."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, str]]] = []

    async def xadd(self, stream: str, fields: dict[str, str]) -> None:
        self.published.append((stream, fields))

    async def aclose(self) -> None:
        return None


def _manifest_raw(permissions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "app_id": _APP_ID,
        "name": "Music Station",
        "version": _VERSION,
        "feature": "waddles.socials.music",
        "module": "socials",
        "provider": "builtin",
        "language": "python",
        "artifact": "source",
        "stages": {
            "process": {
                "entry": "bundles.social_music_process:transform",
                "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
            },
        },
        "permissions": permissions,
    }


def _manifest(permissions: list[dict[str, Any]]) -> Any:
    return parse_bundle_manifest_v2(
        _manifest_raw(permissions),
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


# -- default deny -----------------------------------------------------------


async def test_private_ip_is_instance_denied_by_default(install_dal: Any) -> None:
    assert await svc.is_instance_denied(
        install_dal, permission_id="net.http.private-ip:10.0.0.0/16"
    )
    assert (
        await svc.get_policy_action(install_dal, permission_id="net.http.private-ip:10.0.0.0/16")
        == "deny"
    )


async def test_public_ip_and_fqdn_are_not_instance_denied_by_default(install_dal: Any) -> None:
    assert not await svc.is_instance_denied(install_dal, permission_id="net.http.public-ip:1.2.3.4")
    assert not await svc.is_instance_denied(
        install_dal, permission_id="net.http.fqdn:api.example.com"
    )


async def test_list_policies_empty_by_default(install_dal: Any) -> None:
    # This fixture's sqlite mirror does not run migration 0033's seed
    # INSERT -- `_DEFAULT_DENIED_FAMILIES` is what enforces the fail-closed
    # default here, not an explicit row.
    assert await svc.list_policies(install_dal) == ()


# -- global admin opts in ----------------------------------------------------


async def test_global_admin_can_opt_in_private_ip(install_dal: Any) -> None:
    cascaded = await svc.set_instance_policy(
        install_dal,
        permission_key="net.http.private-ip",
        action="allow",
        set_by=1,
        valkey_client=_FakeValkeyClient(),
    )
    assert cascaded == 0
    assert not await svc.is_instance_denied(
        install_dal, permission_id="net.http.private-ip:10.0.0.0/16"
    )
    policies = await svc.list_policies(install_dal)
    assert policies == (
        svc.InstancePolicy(permission_key="net.http.private-ip", param_scope=None, action="allow"),
    )


async def test_set_instance_policy_writes_audit_row(install_dal: Any) -> None:
    await svc.set_instance_policy(
        install_dal,
        permission_key="net.http.private-ip",
        action="allow",
        set_by=1,
        valkey_client=_FakeValkeyClient(),
    )
    table = install_dal.metadata.tables["instance_permission_policy_audit"]
    async with install_dal.engine.connect() as conn:
        rows = (await conn.execute(select(table))).all()
    assert len(rows) == 1
    assert rows[0].new_action == "allow"
    assert rows[0].cascaded_revocations == 0


# -- enabling a deny cascades revocation ------------------------------------


async def test_enabling_deny_cascades_revocation_of_existing_grants(install_dal: Any) -> None:
    # Opt in first (default is already deny, so flip to allow, grant, then
    # re-deny to exercise the cascade against a real active grant row).
    await svc.set_instance_policy(
        install_dal,
        permission_key="net.http.private-ip",
        action="allow",
        set_by=1,
        valkey_client=_FakeValkeyClient(),
    )
    manifest = _manifest(
        [
            {
                "id": "net.http.private-ip:10.20.0.0/16",
                "justification": "Talks to an internal partner appliance.",
                "params": {"methods": ["GET"]},
            }
        ]
    )
    await perm_svc.record_permission_requests(
        install_dal,
        app_id=_APP_ID,
        version=_VERSION,
        declarations=manifest.permission_declarations,
        approved_by=1,
        approved_permissions=frozenset({"net.http.private-ip:10.20.0.0/16"}),
    )
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme")
    await perm_svc.grant_community_permissions(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=_APP_ID,
        version=_VERSION,
        manifest=manifest,
        granted_permission_ids=frozenset({"net.http.private-ip:10.20.0.0/16"}),
        params_by_id=None,
        granted_by=1,
        valkey_client=_FakeValkeyClient(),
    )
    granted = await perm_svc.get_community_granted_ids(
        install_dal, community_id=community_id, app_id=_APP_ID
    )
    assert granted == frozenset({"net.http.private-ip:10.20.0.0/16"})

    fake_client = _FakeValkeyClient()
    cascaded = await svc.set_instance_policy(
        install_dal,
        permission_key="net.http.private-ip",
        action="deny",
        set_by=1,
        valkey_client=fake_client,
    )
    assert cascaded == 1
    remaining = await perm_svc.get_community_granted_ids(
        install_dal, community_id=community_id, app_id=_APP_ID
    )
    assert remaining == frozenset()
    assert fake_client.published  # invalidation published for the cascaded revoke


async def test_enabling_deny_when_already_denied_does_not_double_cascade(install_dal: Any) -> None:
    # Default is already deny -- flipping deny -> deny again must report 0
    # cascaded revocations (nothing was ever active to revoke).
    cascaded = await svc.set_instance_policy(
        install_dal,
        permission_key="net.http.private-ip",
        action="deny",
        set_by=1,
        valkey_client=_FakeValkeyClient(),
    )
    assert cascaded == 0
