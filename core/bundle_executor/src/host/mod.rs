//! The per-instance host state (`ExecState`) and the two things it wires
//! together: the permitted-for-Rust-components WASI surface (with native
//! `wasi:sockets` denial, assumption A19) and the WIT `stage` world's host
//! imports, serviced over the wire connection to this executor's stage
//! (spec `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
//! SS6.5/SS7.4).

pub mod bridge;
pub mod imports;

use std::sync::Arc;

use tracing::debug;
use wasmtime::component::ResourceTable;
use wasmtime_wasi::{FsPerms, WasiCtx, WasiCtxBuilder, WasiCtxView, WasiView};

pub use bridge::HostBridge;

/// Per-`Store` state: the WASI context and resource table every wasmtime
/// component needs, plus this instance's [`HostBridge`] handle for
/// routing every WIT `stage` world import to the stage over the wire
/// connection (spec SS7.4: every one of `context`/`http`/`kv`/`db`/
/// `relay`/`%flags`/`log`/`clock` is answered stage-side, never locally).
pub struct ExecState {
    wasi_ctx: WasiCtx,
    table: ResourceTable,
    /// `None` only in unit tests that exercise WASI/socket-denial wiring
    /// without a live connection; every call path in `host::imports`
    /// that reaches a WIT import handler requires `Some`.
    pub bridge: Option<Arc<HostBridge>>,
    /// The bundle's `app_id`, attached to every host-call this instance
    /// issues (spec SS6.6's `HostCallBody.app_id`).
    pub app_id: String,
    /// The `invoke` frame id currently in flight, so a host call can be
    /// charged against that invocation's remaining deadline
    /// (`HostCallBody.call_id`, spec SS6.6).
    pub call_id: u64,
}

impl ExecState {
    /// Builds a fresh per-call state: a new [`WasiCtx`] with the
    /// executor's fixed WASI posture (below) and a fresh [`ResourceTable`]
    /// -- spec SS7.2: "Every instance is fresh per call with respect to
    /// guest linear memory: no state survives between invocations."
    pub fn new(bridge: Option<Arc<HostBridge>>, app_id: String, call_id: u64) -> Self {
        Self {
            wasi_ctx: build_wasi_ctx(),
            table: ResourceTable::new(),
            bridge,
            app_id,
            call_id,
        }
    }
}

impl WasiView for ExecState {
    fn ctx(&mut self) -> WasiCtxView<'_> {
        WasiCtxView {
            ctx: &mut self.wasi_ctx,
            table: &mut self.table,
        }
    }
}

/// Builds the `WasiCtx` every loaded bundle instance runs under: the
/// permitted-for-Rust-components surface (empty `wasi:cli` args/env, a
/// single read-only `/scratch` preopen that is empty at load, real
/// `wasi:random`/`wasi:clocks`/`wasi:io`) and, critically, the native
/// `wasi:sockets` denial (assumption A19, spec SS6.5's "wasi:sockets stub
/// rule").
///
/// `allow_tcp(false)`/`allow_udp(false)` make `wasmtime-wasi`'s own,
/// upstream-tested `TcpSocket::new`/`UdpSocket::new` refuse with
/// `io::ErrorKind::PermissionDenied` (which `wasmtime-wasi` maps to
/// `error-code::access-denied`, spec SS6.5 -- the guest Python
/// `PermissionError` the spike observed) **before any OS `socket()`
/// syscall is attempted** -- not merely before `connect`/`bind`.
/// `allow_ip_name_lookup(false)` is `wasmtime-wasi`'s own default, kept
/// explicit here for defense in depth and because a future upstream
/// default change should not silently widen this executor's posture.
///
/// This satisfies "the denying interfaces must be implemented natively in
/// the Rust executor" (spec SS6.5) without hand-authoring the wasi:sockets
/// resource methods from scratch: the Host implementation IS
/// `wasmtime-wasi`'s real one, compiled into this binary (never a
/// separate wasm stub component, which round 2 of the compiler-sandbox
/// spike found impractical) -- what makes it deny everything is this
/// configuration, asserted by this module's own
/// `wasi_tcp_create_socket_is_denied_natively`/
/// `wasi_udp_create_socket_is_denied_natively` tests below.
fn build_wasi_ctx() -> WasiCtx {
    let mut builder = WasiCtxBuilder::new();
    builder
        .allow_tcp(false)
        .allow_udp(false)
        .allow_ip_name_lookup(false)
        // wasi:cli: empty args/env (spec SS6.5's Rust column).
        .args(&[] as &[&str])
        .envs(&[] as &[(&str, &str)])
        .inherit_stdio();

    // wasi:filesystem: a single read-only /scratch preopen, empty at load
    // (spec SS7.1's pod shape mounts `/scratch` as a 16Mi `emptyDir`; this
    // call is what actually exposes it to the guest as a WASI preopen
    // rather than leaving the mount host-side-only). `/scratch` not
    // existing (e.g. a unit test not running under the real pod mount) is
    // not fatal: the guest simply gets no filesystem preopen at all,
    // strictly narrower than the intended posture, never wider.
    if let Err(e) = builder.preopened_dir("/scratch", "/scratch", FsPerms::ReadOnly) {
        debug!(error = %e, "/scratch preopen unavailable, continuing with no filesystem access");
    }

    builder.build()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use wasmtime_wasi::p2::bindings::sockets::network::IpAddressFamily;
    use wasmtime_wasi::p2::bindings::sockets::tcp_create_socket;
    use wasmtime_wasi::sockets::WasiSocketsView;

    #[test]
    fn exec_state_starts_with_no_state_and_a_fresh_table() {
        crate::init_test_tracing();
        let state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        assert_eq!(state.app_id, "waddles.test.app");
        assert_eq!(state.call_id, 1);
        assert!(state.bridge.is_none());
    }

    /// Negative sandbox test #1 (spec SS14.6), isolated to the exact
    /// contract the linker wires up: calling the generated
    /// `tcp_create_socket::Host` trait method directly (the same method
    /// `wasmtime_wasi::p2::add_to_linker_async` binds to the guest's
    /// `wasi:sockets/tcp-create-socket#create-tcp-socket` import) against
    /// an `ExecState` built by `build_wasi_ctx` must fail closed with
    /// `access-denied`, before any socket() syscall, without panicking.
    #[test]
    fn wasi_tcp_create_socket_is_denied_natively() {
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        let mut view = state.sockets();
        let result = tcp_create_socket::Host::create_tcp_socket(&mut view, IpAddressFamily::Ipv4);
        let err = result.expect_err("TCP socket creation must be denied");
        let code = err.downcast();
        assert!(matches!(
            code,
            Ok(wasmtime_wasi::p2::bindings::sockets::network::ErrorCode::AccessDenied)
        ));
    }

    #[tokio::test]
    async fn wasi_udp_create_socket_is_denied_natively() {
        use wasmtime_wasi::p2::bindings::sockets::udp_create_socket;
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        let mut view = state.sockets();
        let result =
            udp_create_socket::Host::create_udp_socket(&mut view, IpAddressFamily::Ipv4).await;
        let err = result.expect_err("UDP socket creation must be denied");
        let code = err.downcast();
        assert!(matches!(
            code,
            Ok(wasmtime_wasi::p2::bindings::sockets::network::ErrorCode::AccessDenied)
        ));
    }
}
