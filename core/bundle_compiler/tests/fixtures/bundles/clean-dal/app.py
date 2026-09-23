"""Fixture bundle already migrated to penguin-dal -- must pass this scan."""
from waddle_sdk.db import get_bundle_dal


async def transform(event):
    dal = get_bundle_dal()
    return None
