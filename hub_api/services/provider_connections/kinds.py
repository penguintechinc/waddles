"""Connection kinds and static-secret schemes: the int codes that are the cross-task contract.

``ConnectionKind`` is the type authority for a ``platform_connections`` row
(``connection_kind SMALLINT``, migration 0064's ``CHECK BETWEEN 1 AND 4``); ``SecretScheme`` names
the shape of a static secret document. Both are int enums (critical-rules: Entity Identity & Typing)
and the numeric values are mirrored by the Rust enums in the data plane -- never renumber one.
"""

from __future__ import annotations

from enum import IntEnum

from services.provider_connections.errors import ProviderConnectionError


class UnknownConnectionKind(ProviderConnectionError):  # noqa: N818 - name fixed by the task contract
    """A stored or supplied connection-kind code is not one this build knows."""

    code = "unknown_connection_kind"


class UnknownSecretScheme(ProviderConnectionError):  # noqa: N818 - sibling of UnknownConnectionKind
    """A supplied secret-scheme code is not one this build knows."""

    code = "unknown_secret_scheme"


class ConnectionKind(IntEnum):
    """What a connection row is: a platform OAuth install or one of three static-secret targets."""

    PLATFORM_OAUTH = 1
    EXTERNAL_HTTP = 2
    AWS_LAMBDA = 3
    GCP_FUNCTION = 4

    @classmethod
    def from_code(cls, code: int) -> ConnectionKind:
        """Decode a stored/supplied int code; raise :class:`UnknownConnectionKind` otherwise."""
        if isinstance(code, bool) or not isinstance(code, int):
            raise UnknownConnectionKind("unknown connection kind")
        try:
            return cls(code)
        except ValueError:
            raise UnknownConnectionKind("unknown connection kind") from None


class SecretScheme(IntEnum):
    """Shape of a static secret document (which keys it must carry)."""

    BEARER = 1
    BASIC = 2
    API_KEY_HEADER = 3
    AWS_ACCESS_KEY = 4
    GCP_SERVICE_ACCOUNT = 5

    @classmethod
    def from_code(cls, code: int) -> SecretScheme:
        """Decode a supplied int code; raise :class:`UnknownSecretScheme` otherwise."""
        if isinstance(code, bool) or not isinstance(code, int):
            raise UnknownSecretScheme("unknown secret scheme")
        try:
            return cls(code)
        except ValueError:
            raise UnknownSecretScheme("unknown secret scheme") from None
