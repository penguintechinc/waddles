"""A scheduled manifest must consume `hub`/`bundle.reconcile` on its own `app_id_slug` source id."""

from __future__ import annotations

import pytest

from services.bundle_manifest_v2 import (
    SCHEDULE_DEFAULT_JITTER,
    ManifestV2Error,
    ScheduleDeclaration,
    app_id_slug,
    parse_bundle_manifest_v2,
)
from tests.test_bundle_manifest_v2 import _VALID_MANIFEST
from tests.test_bundle_manifest_v2_schedule import _with_hub_rule

_APP = str(_VALID_MANIFEST["app_id"])


def _parse(manifest: dict[str, object]) -> object:
    return parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("Waddles.Socials.Music", "waddles-socials-music"),
        ("custom:github", "custom-github"),
        ("---leading-punct", "leading-punct"),
        ("_x_y", "x_y"),
        ("", "x"),
        ("a b", "a-b"),
    ],
)
def test_app_id_slug_mirrors_rust_publisher(raw: str, slug: str) -> None:
    """Same vectors as `core/svc_ingest/src/publish.rs` (dots are NOT kept: they become `-`)."""
    assert app_id_slug(raw) == slug


def test_scheduled_bundle_without_hub_consume_is_refused() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({**_VALID_MANIFEST, "schedule": {"interval_s": 60}})
    assert exc.value.reason == "schedule_requires_hub_consume"


@pytest.mark.parametrize(
    ("source_id", "events", "ok"),
    [
        (None, ("bundle.reconcile",), True),
        (app_id_slug(_APP), ("bundle.reconcile",), True),
        ("someone-elses-stream", ("bundle.reconcile",), False),
        (None, ("chat.message",), False),
    ],
)
def test_hub_consume_rule_must_name_own_source_and_event(
    source_id: str | None, events: tuple[str, ...], ok: bool
) -> None:
    manifest = {**_with_hub_rule(source_id, events), "schedule": {"interval_s": 60}}
    if ok:
        assert _parse(manifest).schedule == ScheduleDeclaration(  # type: ignore[attr-defined]
            60, SCHEDULE_DEFAULT_JITTER
        )
    else:
        with pytest.raises(ManifestV2Error) as exc:
            _parse(manifest)
        assert exc.value.reason == "schedule_requires_hub_consume"
