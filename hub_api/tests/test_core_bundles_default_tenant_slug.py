"""Every `bundles/core-bundles.yaml` activation target names the real catch-all tenant.

regression: migration 0098 renamed the catch-all tenant `global` -> `default`; the
reputation-accrual entry still targeted `global`, a slug no tenant row has, so the
seeder (which never auto-creates tenants) could not activate it.
"""

from pathlib import Path

import yaml
from flask_core.tenancy import DEFAULT_TENANT_SLUG

CATALOG = Path(__file__).resolve().parents[2] / "bundles" / "core-bundles.yaml"


def test_every_activation_target_uses_default_tenant_slug() -> None:
    """Collect every activation target slug; zero examined is a failure, not a pass."""
    doc = yaml.safe_load(CATALOG.read_text())
    entries = doc["bundles"] if isinstance(doc, dict) else doc
    slugs = [
        t["tenant_slug"]
        for e in entries
        for t in (e.get("activation_targets") or [])
        if "tenant_slug" in t
    ]
    assert len(slugs) > 100, f"examined only {len(slugs)} activation targets"
    offenders = sorted({s for s in slugs if s != DEFAULT_TENANT_SLUG})
    assert offenders == [], f"activation targets on unknown tenant slugs: {offenders}"
