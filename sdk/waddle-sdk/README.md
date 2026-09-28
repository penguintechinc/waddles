# waddle-sdk

Python SDK for Waddles app bundles: a `penguin_dal`-compatible database facade
plus `flask_core` compatibility shims, implemented over the
`waddle:bundle/stage@1.0.0` WIT world (`wit/waddle-bundle/stage.wit`).

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

## Testing

```bash
pip install -e ".[dev,build]"
pytest -q --cov=waddle_sdk --cov-report=term-missing --cov-fail-under=90
bash scripts/verify_wit_bindings.sh   # asserts binding shapes vs. real componentize-py output
```
