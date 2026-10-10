"""Strict, fail-closed reads of role/badge flags on untrusted raw ingest events.

Every `is_mod`/`is_subscriber`/`is_owner`/... flag on a raw event decides a
privilege downstream (mod-only commands, broadcaster gates). The raw event
crosses a trust boundary on its way in -- a Valkey list entry decoded with
`json.loads`, or an upstream platform payload -- so the value can be ANY JSON
type, not only a boolean. Python truthiness is the wrong test for it:
`bool("false")` is `True`, so a non-moderator whose producer emitted the
string `"false"` (or `"0"`, `"no"`, a badge name, ...) was promoted to a
moderator. The only value that grants a flag is the real boolean `True`;
everything else -- `False`, `None`, missing, a string, a number, a list -- is
`False` (fail closed).

Same rule `bundles/python/*` adopted for the 40 app bundles (fix/bundle-defects-
wave); enforced statically over this service too by
`scripts/ci/check-bundle-source-hygiene.py --check badge-truthiness`.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def strict_flag(value: Any, *, field: str, platform: str) -> bool:
    """Return `True` only when `value` is the real boolean `True`; anything else is `False`.

    A present-but-non-boolean value (a string such as `"false"`, a number, a
    list, ...) is a producer bug or a spoofing attempt, so it is logged at
    WARNING -- the raw value is NEVER logged (it is attacker-influenced),
    only the platform, the flag name and the value's TYPE. A missing flag
    (`None`) or an explicit `False` is the normal non-privileged case and
    stays silent.
    """
    if value is True:
        return True
    if value is not None and value is not False:
        logger.warning(
            "ingest.badge_not_boolean platform=%s field=%s value_type=%s",
            platform,
            field,
            type(value).__name__,
        )
    return False
