#!/usr/bin/env python3
"""Regenerates tests/fixtures/golden_vector.json from the Python reference primitives.

Uses the same HKDF-SHA256 salt as hub_api/services/envelope/crypto.py and the
same `cryptography` AES-GCM/HMAC the hub-api side uses, so a drift in the Rust
envelope layout, subkey derivation, AAD encoding or blind index fails
tests/golden.rs. Run: python3 -I scripts/gen_golden_vector.py > tests/fixtures/golden_vector.json
"""
import hashlib
import hmac
import json
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_SALT = b"waddles-envelope-v1"  # == hub_api.services.envelope.crypto._HKDF_SALT
MAGIC = bytes([0xFE]) + b"WBD"
ALG = 1


def subkey(dek: bytes, tenant: str, label: str) -> bytes:
    info = f"{label}|tenant:{tenant}".encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=HKDF_SALT, info=info).derive(dek)


def field(value: str) -> bytes:
    raw = value.encode()
    return struct.pack(">I", len(raw)) + raw


def aad(parts: list[str], dek_version: int) -> bytes:
    return b"".join(field(p) for p in parts) + field(str(dek_version))


def main() -> None:
    dek = bytes(range(1, 33))
    tenant = "42"
    parts = ["42", "7", "waddles.bot.a", "kv", "counters.total"]
    version = 3
    nonce = bytes(range(0xA0, 0xAC))
    plaintext = b'{"hello":"world"}'
    data_key = subkey(dek, tenant, "waddles-bundle-data-v1")
    ct = AESGCM(data_key).encrypt(nonce, plaintext, aad(parts, version))
    envelope = MAGIC + bytes([ALG]) + struct.pack(">I", version) + nonce + ct

    bidx_key = subkey(dek, tenant, "waddles-bundle-bidx-v1")
    ctx = b"".join(field(p) for p in ["7", "waddles.bot.a", "users", "handle"])
    bidx = hmac.new(bidx_key, ctx + b"alice", hashlib.sha256).digest()

    print(
        json.dumps(
            {
                "dek_hex": dek.hex(),
                "tenant_id": tenant,
                "aad_parts": parts,
                "dek_version": version,
                "nonce_hex": nonce.hex(),
                "plaintext_hex": plaintext.hex(),
                "data_subkey_hex": data_key.hex(),
                "envelope_hex": envelope.hex(),
                "blind_context": ["7", "waddles.bot.a", "users", "handle"],
                "blind_value_hex": b"alice".hex(),
                "blind_index_hex": bidx.hex(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
