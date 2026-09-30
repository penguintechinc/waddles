// Minimal `connector@1.0.0` world component (spec
// `docs/superpowers/specs/2026-09-28-connector-bundles.md` S1) used by
// `core/bundle_executor/tests/linker_isolation.rs` to prove the
// per-component `Linker`'s S3.2.1 gate 2: `on-connect` calls
// `identity::lookup` unconditionally, so this compiled component's import
// set genuinely requires `identity.lookup` to be linked -- instantiating it
// against a `Linker` built for a manifest that does not pass
// `VerifiedManifest::may_link_identity` must fail with wasmtime's standard
// "unknown import" error, and against one that does pass must succeed.
#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
use bindings::exports::waddle::connector::sender::{
    Action, Guest as SenderGuest, HttpRequestTpl,
};
use bindings::waddle::connector::identity::{self, IdentityKey};
use bindings::waddle::connector::types::{
    ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
};

struct Component;

impl ReceiverGuest for Component {
    fn on_connect(ctx: ConnectionCtx) -> Result<HandshakePayload, FrameError> {
        // Force a genuine `identity.lookup` import -- this is the whole
        // point of this fixture (see module doc comment above).
        let _ = identity::lookup(&IdentityKey::PlatformIdentity((
            ctx.platform.clone(),
            ctx.source_id.clone(),
        )));
        Ok(HandshakePayload {
            bytes: Vec::new(),
            secret_refs: Vec::new(),
        })
    }

    fn on_frame(_frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
        Ok(Vec::new())
    }

    fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
        Ok(None)
    }

    fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {}
}

impl SenderGuest for Component {
    fn build_request(_action: Action) -> Result<HttpRequestTpl, FrameError> {
        Err(FrameError::Backend("not implemented in fixture".to_string()))
    }
}

bindings::export!(Component with_types_in bindings);
