# waddle-sdk

Python SDK for Waddles app bundles: a `penguin_dal`-compatible database facade
plus `flask_core` compatibility shims, implemented over the
`waddle:bundle/stage@1.0.0` WIT world (`wit/waddle-bundle/stage.wit`).

See [`AUTHORING.md`](AUTHORING.md) for the standard `!<command>` grammar
(`waddle_sdk.command`), default-OFF sub-modules (`waddle_sdk.sub_modules`),
and community-scoped state (`waddle_sdk.community_kv`).

## Writing a bundle

A bundle author writes only plain, testable module-level coroutines and
never touches this SDK's componentize-py wiring directly:

```python
# app.py
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

async def transform(event: PlatformEvent) -> PlatformEvent | None:
    ...  # process-stage.transform

async def dispatch(envelope: StageEnvelope, config: dict, *, http_client) -> Any:
    ...  # action-stage.dispatch
```

## Building the component

`componentize-py componentize`'s app module is always
`waddle_sdk._component_entry` — never a bundle's own module. It exposes two
separate app classes, `ProcessStage` and `ActionStage`, because
`world stage` exports two separate interfaces
(`process-stage` + `action-stage`); componentize-py 0.25.1 resolves each
exported interface by looking up a module attribute named after it
(`getattr(app_module, "ProcessStage")` / `"ActionStage"`), so a single
combined class fails componentization outright. See
`_component_entry.py`'s module docstring for the full explanation and the
confirmed real generated binding shapes.

`_component_entry` wires those classes to a bundle's `transform`/`dispatch`
via a static `_entry_wiring.py` module, generated at build time by
`bundle_compiler`'s `generate_entry_wiring()` (from `bundle.yaml`'s
`stages.<s>.entry`) into the bundle's own source directory — see
`bundles/python/pyping/src/_entry_wiring.py` for the hand-authored shape a
bundle not routed through the compiler provides instead:

```python
# _entry_wiring.py
from app import transform as bundle_transform
from app import dispatch as bundle_dispatch
```

Build command (pins match `pyproject.toml`'s `[build]` extra):

```bash
componentize-py -d wit/waddle-bundle -w stage componentize \
  -p sdk/waddle-sdk/src -p bundles/python/<bundle>/src \
  waddle_sdk._component_entry -o <bundle>.wasm
```

Verify both interfaces were exported:

```bash
wasm-tools component wit <bundle>.wasm | grep '^  export waddle:bundle'
# export waddle:bundle/process-stage@1.0.0;
# export waddle:bundle/action-stage@1.0.0;
```

## Outbound HTTP permissions

`dispatch()`'s `http_client` (and any other outbound call your bundle makes)
is gated by the manifest's `permissions:` block
(`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
§1.1). Declare a **stable public hostname** with `net.http.fqdn:<host>` —
this is the preferred, lowest-risk form and the one you should reach for
first. `net.http.public-ip:<ip>` (a bare IP, no FQDN) is high risk.
**Bundles should never see or target private IP addresses** —
`net.http.private-ip:<ip|cidr>` exists only for the exceptional self-hosted/
on-prem case, requires its own separate explicit approval, is flagged high
risk on every consent screen, and is never available for the platform's own
cluster networks (loopback, link-local, metadata, and this cluster's own
pod/service/node CIDRs are always denied, regardless of grant). Reviewers
and admins should treat any `net.http.private-ip` request in your manifest
as a red flag, so avoid it unless there is genuinely no alternative.

## Testing

```bash
pip install -e ".[dev,build]"
pytest -q --cov=waddle_sdk --cov-report=term-missing --cov-fail-under=90
bash scripts/verify_wit_bindings.sh   # asserts binding shapes vs. real componentize-py output
```
