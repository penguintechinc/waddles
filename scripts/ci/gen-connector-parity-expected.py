#!/usr/bin/env python3
"""Generate / verify the connector-overlay parity expectations.

Source of truth is the existing data-plane corpus (`tests/data_plane_parity/corpus`):
each vector's `golden` is the output of the hardcoded normalizers (Rust oracle
`core/svc_ingest/src/{normalize,ingest/*}.rs`, blessed via `PARITY_BLESS`, kept
current by the `Data-Plane Parity` CI job; YouTube = frozen Python output). This
script only RESHAPES each golden into what a connector overlay's
`receiver.on-frame` must emit, writing `tests/data_plane_parity/expected/
<platform>/<name>.json`:

    input   : the exact bytes handed to on-frame (+ optional harness context)
    outcome : "event" | "dropped"        (frame_error is hand-written, fixtures only)
    events  : [{platform, event_type, actor, payload}]  (occurred_at: shape-checked)

`actor` is TOKENIZED the way a connector must (S3.3): the raw handle in the golden is
replaced by the harness identity stub's pseudonym for (platform, platform user id).

Usage:  gen-connector-parity-expected.py [--check] [--corpus DIR] [--out DIR]
  default  write expected files (prints counts)
  --check  fail (exit 1) if any committed file differs from regeneration, is missing,
           or is orphaned. Zero vectors examined is a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "tests" / "data_plane_parity" / "corpus"
OUT = REPO / "tests" / "data_plane_parity" / "expected"
PLATFORMS = ("twitch", "discord", "youtube")

#: Candidate payload fields holding the actor's platform id, first present wins, per corpus path;
#: no id present -> the raw handle is the identity key.
ACTOR_ID_FIELD = {
    "twitch_irc": ("user_id",),
    "twitch_eventsub": ("user_id", "broadcaster_id"),
    "discord_gateway": ("author_id",),
    "youtube_live_poll": ("author_id",),
}


def pseudonym_uuid(platform: str, platform_user_id: str) -> str:
    """Mirror of `bundle_executor::parity_support::pseudonym_uuid` (kept in sync by a Rust test)."""
    d = hashlib.sha256(
        b"waddles-parity-v1\0" + platform.encode() + b"\0" + platform_user_id.encode()
    ).digest()[:16]
    b = bytearray(d)
    b[6] = (b[6] & 0x0F) | 0x50
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def encode_input(vector: dict) -> dict:
    """The single place that decides what bytes an overlay's on-frame receives."""
    frame, path = vector["frame"], vector["path"]
    if path == "discord_gateway":
        # Generic WS transport hands the overlay raw gateway dispatch frames.
        frame = {"op": 0, "t": "MESSAGE_CREATE", "s": 1, "d": frame}
    if isinstance(frame, str):
        return {"encoding": "utf8", "text": frame}
    return {"encoding": "json", "text": json.dumps(frame, separators=(",", ":"), ensure_ascii=False)}


def build(vector: dict) -> dict:
    """Reshape one corpus vector into an expected-overlay-output record."""
    out = {
        "id": vector["id"],
        "platform": vector["platform"],
        "path": vector["path"],
        "input": encode_input(vector),
        "context": vector.get("context") or {},
        "outcome": vector["outcome"],
        "events": [],
    }
    if vector["outcome"] == "event":
        g = vector["golden"]
        actor = None
        if g.get("actor") is not None:
            fields = ACTOR_ID_FIELD[vector["path"]]
            uid = next((p[f] for f in fields if p.get(f) is not None), None) if (p := g.get("payload") or {}) is not None else None
            if uid is None:
                uid = g["actor"]  # no id on the wire (e.g. untagged IRC): the login is the identity key
            actor = {"platform_user_id": str(uid), "uuid": pseudonym_uuid(g["platform"], str(uid)), "raw_handle": g["actor"]}
        out["events"].append(
            {
                "platform": g["platform"],
                "event_type": g["event_type"],
                "actor": actor,
                "payload": g["payload"],
            }
        )
    return out


def render(rec: dict) -> str:
    return json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def expected_set(corpus: Path) -> dict[str, str]:
    """relative path -> rendered content, for every corpus vector of the 3 overlay platforms."""
    result: dict[str, str] = {}
    for platform in PLATFORMS:
        for f in sorted((corpus / platform).glob("*.json")):
            vec = json.loads(f.read_text())
            result[f"{platform}/{f.name}"] = render(build(vec))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()

    want = expected_set(a.corpus)
    if not want:
        print("[connector-parity-gen] FAIL: zero corpus vectors examined", file=sys.stderr)
        return 1
    if not a.check:
        for rel, text in want.items():
            p = a.out / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        print(f"[connector-parity-gen] wrote {len(want)} expected files")
        return 0

    bad: list[str] = []
    for rel, text in want.items():
        p = a.out / rel
        if not p.is_file():
            bad.append(f"missing  {rel}")
        elif p.read_text() != text:
            bad.append(f"stale    {rel}")
    have = {f"{p.parent.name}/{p.name}" for plat in PLATFORMS for p in (a.out / plat).glob("*.json") if p.name != "divergences.json"}
    bad += [f"orphaned {rel}" for rel in sorted(have - set(want))]
    if bad:
        print("[connector-parity-gen] FAIL: expected files out of date (run `make gen-connector-parity-expected`):", file=sys.stderr)
        print("\n".join("  " + b for b in bad), file=sys.stderr)
        return 1
    print(f"[connector-parity-gen] OK examined={len(want)} current={len(want)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
