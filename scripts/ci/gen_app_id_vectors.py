"""Regenerate `testdata/bundle_app_id/vectors.json` (golden app_id vectors).

The accept/reject/core lists are hand-curated here; the `storage_keys` are produced by the Python
`bundle_storage_key`, and the Rust crate `bundle_capability_gate` independently asserts the SAME
keys, so a divergence between the two implementations fails one of the two test suites. Run from
the repo root only when the algorithm intentionally changes:
`python3 scripts/ci/gen_app_id_vectors.py`.
"""

import json
import os
import sys

sys.path.insert(0, "hub_api")
from services.bundle_app_id import bundle_storage_key

valid = [
    "penguintechinc.waddles.echo",
    "penguintechinc.waddles.discord-invite",
    "psychoboy.penguintwitchbot.roll",
    "acme.bot.kv-store",
    "acme.bot-kv.store",
    "0.0.0",
    "a.b.c",
    "a-.b-.c-",
    "a" * 40 + "." + "b" * 40 + "." + "c" * 45,
]
invalid = [
    "",
    "a.b",
    "a.b.c.d",
    "a..c",
    ".a.b",
    "a.b.",
    "acme.bot_kv.store",
    "acme.bot.kv_store",
    "penguintechinc.waddles.fixture_bot",
    "-a.b.c",
    "a.-b.c",
    "a.b.-c",
    "A.b.c",
    "a.b.C",
    "a.b.c\n",
    "a.b.c ",
    "a:b.c.d",
    "a/b.c.d",
    "a.b.c%41",
    "ü.b.c",
    "waddles.core.x.y",
    "a" * 50 + "." + "b" * 50 + "." + "c" * 50,
]
core_yes = [
    "penguintechinc.waddles.echo",
    "penguintechinc.waddles.discord-invite",
    "penguintechinc.waddles.x",
    "penguintechinc.waddles.0",
]
core_no = [
    "",
    "waddles.core.x",
    "penguintechinc.waddles.",
    "penguintechinc.waddles.a.b",
    "penguintechinc.waddles.-x",
    "penguintechinc.waddles.X",
    "penguintechinc.waddles.x_y",
    "penguintechinc.waddles.x\n",
    "penguintechinc.other.x",
    "psychoboy.penguintwitchbot.roll",
    "penguintechinc.waddlesx",
    "penguintechinc.waddles",
    "xpenguintechinc.waddles.x",
]
key_ids = [
    "penguintechinc.waddles.echo",
    "penguintechinc.waddles.discord-invite",
    "psychoboy.penguintwitchbot.roll",
    "acme.bot.kv-store",
    "acme.bot_kv.store",
    "acme.bot-kv.store",
    "0.0.0",
    "a-.b-.c-",
    "UP.per.Case",
    "a" * 50 + "." + "b" * 50 + "." + "c" * 25,
    "a" * 50 + "." + "b" * 50 + "." + "d" * 25,
    "",
    "..",
    "ü.b.c",
    "a.b.c\n",
]
out = {
    "_comment": "Golden vectors read by BOTH core/bundle_capability_gate (cargo test) and hub_api/tests/test_bundle_app_id.py. storage_keys are sha256-derived; regenerate with scripts/ci/gen_app_id_vectors.py only when the algorithm intentionally changes.",
    "valid": valid,
    "invalid": invalid,
    "core": {"yes": core_yes, "no": core_no},
    "storage_keys": [{"app_id": i, "expected": bundle_storage_key(i)} for i in key_ids],
}
os.makedirs("testdata/bundle_app_id", exist_ok=True)
with open("testdata/bundle_app_id/vectors.json", "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=True)
    f.write("\n")
