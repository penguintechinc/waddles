"""Authentication-methods (`amr`, RFC 8176) values for the authentication events hub-api verifies.

The access token's `amr` / `auth_time` say HOW and WHEN the user last proved who they are, and
`services/mfa_stepup.py` trusts them for the most sensitive admin actions. Only values the server
itself established are ever minted here:

  password login ........................ `["pwd"]`                 one factor, never MFA
  passkey, user NOT verified ............ `["hwk"|"swk"]`           possession only, never MFA
  passkey, user verified (PIN/biometric)  `["hwk"|"swk","user","mfa"]`  two factors: MFA

`hwk` = a device-bound (hardware) credential, `swk` = a synced software credential. `mfa` is added
ONLY when the authenticator itself reported user verification (the WebAuthn UV flag, checked by
the server on the signed authenticator data), so a stolen session or a passkey touched without
verification can never satisfy the MFA gate.
"""

from __future__ import annotations

from typing import Final

PASSWORD: Final = "pwd"  # noqa: S105 -- an RFC 8176 amr value, not a credential
HARDWARE_KEY: Final = "hwk"
SOFTWARE_KEY: Final = "swk"
USER_PRESENCE: Final = "user"
MULTI_FACTOR: Final = "mfa"


def passkey_amr(*, user_verified: bool, single_device: bool) -> list[str]:
    """`amr` for a verified passkey assertion (see the module table)."""
    method = HARDWARE_KEY if single_device else SOFTWARE_KEY
    return [method, USER_PRESENCE, MULTI_FACTOR] if user_verified else [method]
