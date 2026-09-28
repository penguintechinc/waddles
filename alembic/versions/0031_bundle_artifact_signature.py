"""`app_versions` artifact-signature columns (spec SS5.6, Gemini review condition 9).

Adds `artifact_signature`/`artifact_signature_key_id`/
`artifact_signed_approval_id`/`artifact_signed_at` to `app_versions`
(migration 0022, extended by 0024's `component_key`/`sidecar_key`) --
`services/bundle_signing_service.py` writes all four in the SAME
transaction as the approval it signs
(`bundle_approval_service._write_approval_and_activate()`), so a
successfully-committed `app_install_approvals` row always has a matching
signature on its `app_versions` row, never a partially-signed state.

`artifact_signature` is the base64 Ed25519 signature over
`build_signing_payload(app_id, version, digest, approval_id)` (NUL-
separated, NOT canonical JSON -- see that function's own docstring for
why); `artifact_signature_key_id` names which configured platform key
signed it (supports rotation, spec SS5.6); `artifact_signed_approval_id`
is a real FK to the exact `app_install_approvals` row the signature was
computed against -- re-fetching that row (rather than trusting "whichever
approval happens to be current now") is what lets a verifier reconstruct
the exact payload that was signed, independent of any LATER approval for
the same `(app_id, version)`. All four are nullable: a `source`-artifact
row (not yet built, see `bundle_version_service.py`'s own scope note) or a
`prebuilt` row from before this migration/its approval-time signing
landed has `artifact_signature IS NULL` -- `core/bundle_executor`'s
sidecar-signature check (`crate/signing.rs`) fails closed on exactly that
case, it is not this migration's job to backfill it (see
`hub_api/cli/sign_approved_bundles.py` for the one-off backfill CLI).

**Stacking note (coordinator/reviewer-visible):** written as
`down_revision = "0030_bundle_app_schemas"` per this milestone's plan, but
`0030_bundle_app_schemas` does not exist on this branch's `release/v3.0.X`
base yet (a separate, in-flight PR) -- this migration is stacked on top of
it and WILL need renumbering (and possibly a `down_revision` fix-up) at
merge time once `0030` actually lands, exactly like migration 0024's own
precedent for landing two schema-owning changes in parallel.

Revision ID: 0031_bundle_artifact_signature
Revises: 0030_bundle_app_schemas
Create Date: 2026-09-28
"""

from __future__ import annotations

from alembic import op

revision = "0031_bundle_artifact_signature"
down_revision = "0030_bundle_app_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app_versions
          ADD COLUMN IF NOT EXISTS artifact_signature TEXT,
          ADD COLUMN IF NOT EXISTS artifact_signature_key_id VARCHAR(100),
          ADD COLUMN IF NOT EXISTS artifact_signed_approval_id BIGINT
              REFERENCES app_install_approvals(id),
          ADD COLUMN IF NOT EXISTS artifact_signed_at TIMESTAMPTZ
        """
    )
    op.execute(
        "COMMENT ON COLUMN app_versions.artifact_signature IS "
        "'base64 Ed25519 signature over build_signing_payload(app_id, version, artifact_digest, "
        "artifact_signed_approval_id) -- spec SS5.6, Gemini review condition 9'"
    )
    op.execute(
        "COMMENT ON COLUMN app_versions.artifact_signature_key_id IS "
        "'which platform signing key produced artifact_signature -- supports rotation'"
    )
    op.execute(
        "COMMENT ON COLUMN app_versions.artifact_signed_approval_id IS "
        "'app_install_approvals.id the signature was computed against -- re-fetch this exact row "
        "to reconstruct the signed payload, never \"whichever approval is current now\"'"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app_versions
          DROP COLUMN IF EXISTS artifact_signed_at,
          DROP COLUMN IF EXISTS artifact_signed_approval_id,
          DROP COLUMN IF EXISTS artifact_signature_key_id,
          DROP COLUMN IF EXISTS artifact_signature
        """
    )
