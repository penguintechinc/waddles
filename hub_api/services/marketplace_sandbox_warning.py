"""Versioned sandbox-build trust warning + its hash (design 4.8).

The operator must acknowledge the exact current warning text to add a SANDBOX source; the ack
carries the SHA-256 of this text, so a stale (older-version) or fabricated ack is refused.
Bumping `WARNING_VERSION`/text changes the hash and invalidates every client-held ack.
"""

from __future__ import annotations

import hashlib

WARNING_VERSION = 1

WARNING_TEXT = (
    "Building a marketplace source runs its code on your platform. Sandboxed build compiles "
    "source code from the source git URL at the acknowledged commit inside Waddles "
    "infrastructure. That code is untrusted and could attempt to compromise or disrupt the "
    "platform. Builds run isolated (no network, no privileges, resource-limited) but isolation "
    "is not a guarantee. Only add sources you trust and have reviewed. Every build result still "
    "requires your explicit approval before it is installed, and you remain responsible for what "
    "you approve. I understand and accept this risk for this source."
)

WARNING_HASH = hashlib.sha256(f"v{WARNING_VERSION}\n{WARNING_TEXT}".encode()).hexdigest()
