#!/usr/bin/env python3
"""Regenerates tests/fixtures/hpke_vector.json: the hub-api tenant-DEK seal.

Implements the construction documented in proto/waddles/hub/internal/v1/key.proto
with the `cryptography` library (the same one hub-api uses), from fixed test
keys: X25519 ECDH, HKDF-SHA256 (no salt, info = context) -> 44 bytes
(32-byte key || 12-byte nonce), AES-256-GCM with aad = context. The Rust unit
test `hpke_open_matches_python_reference_vector` opens it.
Run: python3 -I scripts/gen_hpke_vector.py > tests/fixtures/hpke_vector.json
"""
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def main() -> None:
    client = X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    server = X25519PrivateKey.from_private_bytes(bytes(range(101, 133)))
    shared = server.exchange(client.public_key())
    assert shared == client.exchange(server.public_key())
    context = "svc-process|42|at-rest|1"
    okm = HKDF(
        algorithm=hashes.SHA256(), length=44, salt=None, info=context.encode()
    ).derive(shared)
    key, nonce = okm[:32], okm[32:]
    dek = bytes(range(0xC0, 0xE0))
    ct = AESGCM(key).encrypt(nonce, dek, context.encode())
    assert len(ct) == 48
    print(
        json.dumps(
            {
                "context": context,
                "shared_hex": shared.hex(),
                "dek_hex": dek.hex(),
                "ciphertext_hex": ct.hex(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
