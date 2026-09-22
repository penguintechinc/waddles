"""Fixture bundle that still imports the legacy DAL -- must be rejected."""
from flask_core.database import AsyncDAL
import pydal


async def transform(event):
    dal = AsyncDAL()
    return None
