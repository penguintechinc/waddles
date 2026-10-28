#!/usr/bin/env python3
"""Regenerate the Python-side parity vectors for `core/waddles_ops_cli`.

The Rust `waddles-ops` tool must stay byte-identical to the Python it supersedes
(`hub_api/services/{bundle_signing_service,audit_chain,suppression_keys}.py`). This script runs
the REAL Python modules and writes the vectors the Rust parity tests replay:

    python3 -I scripts/gen_ops_cli_vectors.py            # from the repo root
    git diff --exit-code core/waddles_ops_cli/tests/*.json   # CI: vectors must be reproducible

Needs only `cryptography` (already a hub-api dependency). Deterministic: fixed seeds/KEKs and
fixed timestamps, so a second run is byte-identical. Dev-only material: every key below is a
throwaway test value, never a real secret.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUB = ROOT / "hub_api"
OUT = ROOT / "core" / "waddles_ops_cli" / "tests"


def _stub(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


def _package(name: str, path: Path) -> None:
    """Register `name` as a bare package so submodules import without running `__init__`."""
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    sys.modules[name] = mod


def _load_signing():
    """Load bundle_signing_service with its DB/storage imports stubbed (signing is pure)."""

    class ApiError(Exception):
        def __init__(self, *args: object) -> None:
            super().__init__(*args)

    _package("services", HUB / "services")
    _stub("services.storage_service")
    sys.modules["services"].storage_service = sys.modules["services.storage_service"]
    _stub("services.errors", ApiError=ApiError)
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        _stub("sqlalchemy", Table=object, update=None)
        _stub("sqlalchemy.future", select=None)
    spec = importlib.util.spec_from_file_location(
        "bundle_signing_service", HUB / "services" / "bundle_signing_service.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bundle_signing_service"] = mod
    spec.loader.exec_module(mod)
    return mod


def signing_vectors() -> list[dict]:
    from cryptography.hazmat.primitives import serialization as S

    b = _load_signing()
    out = []
    cases = [
        (7, ("core-ping", "1.0.0", "sha256:" + "ab" * 32, 42)),
        (9, ("a.b_c-d", "0.0.1-rc.1", "cd" * 32, 1)),
        (1, ("x", "1", "sha256:" + "00" * 32, 2**40 + 5)),
    ]
    for seed_byte, (a, v, d, i) in cases:
        seed = bytes([seed_byte]) * 32
        os.environ["BUNDLE_SIGNING_PRIVATE_KEY"] = base64.b64encode(seed).decode()
        os.environ["BUNDLE_SIGNING_KEY_ID"] = "k1"
        s = b.PlatformSigner.from_env()
        sig, kid = s.sign(app_id=a, version=v, digest=d, approval_id=i)
        pub = s._private_key.public_key().public_bytes(S.Encoding.Raw, S.PublicFormat.Raw)
        doc = {
            "app_id": a, "version": v, "digest": d, "approval_id": i,
            "key_id": kid, "algorithm": "ed25519", "signature": sig,
        }
        out.append({
            "seed_b64": os.environ["BUNDLE_SIGNING_PRIVATE_KEY"],
            "pub_b64": base64.b64encode(pub).decode(),
            "app_id": a, "version": v, "digest": d, "approval_id": i,
            "payload_hex": b.build_signing_payload(
                app_id=a, version=v, digest=d, approval_id=i).hex(),
            "sidecar": json.dumps(doc, sort_keys=True, separators=(",", ":")),
        })
    return out


def _load_envelope_modules():
    _package("services", HUB / "services")
    _package("services.envelope", HUB / "services" / "envelope")
    from services import audit_chain, suppression_keys  # noqa: E402

    return audit_chain, suppression_keys


HOSTILE_DETAILS = [
    {},
    {"k": "plain", "a": 1, "z": None, "b": True, "c": False},
    {"u": "caf\u00e9", "emoji": "x\U0001f600y", "nul": "a\u0000b", "del": "a\x7fb"},
    {"q": 'he said "hi"\\n', "ctl": "\n\r\t\b\f\x1f\x01", "sep": "\u2028\u2029"},
    {"big": 2**63 - 1, "neg": -(2**63), "zero": 0, "lst": [1, "a\u00ff", None, True, []]},
    {"\u00e9key": 1, "Zkey": 2, "akey": 3, "\U0001f600k": 4, "\uffffk": 5},
]


def audit_vectors() -> dict:
    ac, _ = _load_envelope_modules()
    canon = []
    for n, details in enumerate(HOSTILE_DETAILS):
        rec = ac.ChainRecord(
            chain_id="tenant:7" if n % 2 else "platform",
            seq=n + 1,
            event_id=f"00000000-0000-4000-8000-{n:012d}",
            occurred_at="2026-10-18T01:02:03.004500Z",
            actor_uuid=None if n % 3 == 0 else "11111111-2222-4333-8444-555555555555",
            actor_kind="user", category="auth", action="login.success", outcome="success",
            target_type=None if n % 2 else "user", target_id=None if n % 2 else "t\u00e9",
            details=details, prev_hash=ac.GENESIS_HASH, record_hash="",
        )
        canon.append({
            "record": _rec_json(rec),
            "canonical_hex": ac.canonical_bytes(rec).hex(),
            "hash": ac.compute_hash(rec),
        })

    # Chains are also inserted into Postgres by tests/db_integration.rs; JSONB cannot store
    # U+0000, so the chain details exclude the NUL case (it stays in the canonical vectors).
    chain_details = [d for d in HOSTILE_DETAILS if "nul" not in d]

    def make_chain(n: int, chain_id: str) -> list:
        recs, prev = [], ac.GENESIS_HASH
        for i in range(1, n + 1):
            r = ac.seal(ac.ChainRecord(
                chain_id=chain_id, seq=i, event_id=f"00000000-0000-4000-8000-{i:012d}",
                occurred_at=f"2026-10-18T00:00:{i:02d}.000001Z", actor_uuid=None,
                actor_kind="system", category="bundle", action="bundle.approve",
                outcome="success", target_type="app", target_id=f"app{i}@1.0.0",
                details=chain_details[i % len(chain_details)], prev_hash=prev,
                record_hash=""))
            recs.append(r)
            prev = r.record_hash
        return recs

    def mutate(rec, **kw):
        d = {f: getattr(rec, f) for f in rec.__dataclass_fields__}
        d.update(kw)
        return ac.ChainRecord(**d)

    base = make_chain(5, "tenant:7")
    head = base[-1]
    scenarios = {
        "intact": (base, {}),
        "intact_pinned": (base, {"expected_head_seq": 5, "expected_head_hash": head.record_hash}),
        "head_seq_mismatch": (base[:4], {"expected_head_seq": 5}),
        "head_hash_mismatch": (base, {"expected_head_hash": "f" * 64}),
        "tampered_field": ([*base[:2], mutate(base[2], outcome="failure"), *base[3:]], {}),
        "seq_gap": ([*base[:2], *base[3:]], {}),
        "prev_mismatch": ([base[0], base[2], base[1], *base[3:]], {}),
        "wrong_chain": ([*base[:1], mutate(base[1], chain_id="tenant:8")], {}),
        "bad_version": ([mutate(base[0], hash_version="sha256-v2")], {}),
        "malformed_hash": ([mutate(base[0], record_hash="XYZ")], {}),
        "empty": ([], {}),
        "empty_pinned": ([], {"expected_head_seq": 1}),
    }
    chains = []
    for name, (recs, kw) in scenarios.items():
        res = ac.verify_records(recs, chain_id="tenant:7", **kw)
        chains.append({
            "name": name,
            "records": [_rec_json(r) for r in recs],
            "pin_seq": kw.get("expected_head_seq"),
            "pin_hash": kw.get("expected_head_hash"),
            "status": res.status.value,
            "examined": res.examined,
            "head_seq": res.head_seq,
            "head_hash": res.head_hash,
            "break_seq": res.break_.seq if res.break_ else None,
            "break_reason": res.break_.reason.value if res.break_ else None,
            "break_detail": res.break_.detail if res.break_ else None,
        })
    return {"canonical": canon, "chains": chains}


def _rec_json(r) -> dict:
    return {f: getattr(r, f) for f in r.__dataclass_fields__}


def suppression_vectors() -> dict:
    _, sk = _load_envelope_modules()
    from services.envelope.platform_kek import PlatformKekAdapter

    cur, prev = bytes(range(32)).hex(), bytes(range(32, 64)).hex()
    src = sk.PlatformKekKeySource({"TENANT_KEK_HEX": cur, "TENANT_KEK_HEX_PREVIOUS": prev})
    identities = [
        (1, "discord", "123456789012345678"),
        (1, "  Discord ", "  123456789012345678\t"),
        (2, "discord", "123456789012345678"),
        (2, "twitch", "CaseSensitiveName"),
        (42, "kick", "caf\u00e9-\U0001f600"),
        (7, "\u212aick", "u1"),          # Kelvin sign lower-cases to ASCII 'k'
        (7, "kick", "\x1cpadded\x1f"),   # Python str.strip() also strips U+001C..U+001F
        (7, "kick", "\u00a0nbsp\u3000"),
        (9, "a" * 50, "x" * 255),
    ]
    codes = []
    for tenant, platform, puid in identities:
        keys = src.tenant_keys(tenant)
        plat, norm = sk.normalize_identity(platform, puid)
        codes.append({
            "tenant_id": tenant, "platform": platform, "platform_user_id": puid,
            "norm_platform": plat, "norm_platform_user_id": norm,
            "tenant_key_vers": [k.key_ver for k in keys],
            "codes": [
                {"key_ver": c.key_ver, "hmac": c.hmac}
                for c in sk.lookup_codes(src, tenant, platform, puid)
            ],
        })
    invalid = []
    for tenant, platform, puid in [
        (0, "discord", "1"), (-1, "discord", "1"), (1, "", "1"), (1, "9bad", "1"),
        (1, "Bad-Plat", "1"), (1, "discord", ""), (1, "discord", "   "),
        (1, "discord", "a\x00b"), (1, "discord", "a\x7fb"), (1, "discord", "a\nb"),
        (1, "discord", "x" * 256), (1, "a" * 51, "1"),
    ]:
        try:
            sk.compute_code(sk.TenantKey("v", b"k" * 32), platform, puid)
            sk.validate_tenant_id(tenant)
            raise AssertionError("expected rejection")
        except sk.SuppressionInputError:
            invalid.append({"tenant_id": tenant, "platform": platform, "platform_user_id": puid})
    ids = {"cur": PlatformKekAdapter(bytes.fromhex(cur)).kek_ids[0]}
    return {
        "kek_hex": cur, "kek_previous_hex": prev,
        "kek_ids": list(PlatformKekAdapter(bytes.fromhex(cur), bytes.fromhex(prev)).kek_ids),
        "kek_id_current": ids["cur"],
        "codes": codes,
        "invalid": invalid,
    }


def sidecar_vectors() -> list[dict]:
    """Sidecar bodies with an unvalidated, non-ASCII `digest` (Python default ensure_ascii)."""
    out = []
    for digest in ["sha256:" + "ab" * 32, "sha256:caf\u00e9-\U0001f600-q\"\\\x7f"]:
        doc = {
            "app_id": "a", "version": "1", "digest": digest, "approval_id": 3,
            "key_id": "k1", "algorithm": "ed25519", "signature": "c2ln",
        }
        out.append({"doc": doc, "body": json.dumps(doc, sort_keys=True, separators=(",", ":"))})
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = {
        "python_vectors.json": signing_vectors,
        "audit_vectors.json": audit_vectors,
        "suppression_vectors.json": suppression_vectors,
        "sidecar_vectors.json": sidecar_vectors,
    }
    for name, fn in jobs.items():
        (OUT / name).write_text(json.dumps(fn(), indent=1, ensure_ascii=True) + "\n")
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
