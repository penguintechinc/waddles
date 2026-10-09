//! The per-instance host state (`ExecState`) and the two things it wires
//! together: the permitted-for-Rust-components WASI surface (with native
//! `wasi:sockets` denial, assumption A19) and the WIT `stage` world's host
//! imports, serviced over the wire connection to this executor's stage
//! (spec `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
//! SS6.5/SS7.4).

pub mod bridge;
pub mod connector_imports;
pub mod imports;
pub mod stage_next_imports;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use tracing::debug;
use wasmtime::component::ResourceTable;
use wasmtime::{ResourceLimiter, StoreLimits, StoreLimitsBuilder};
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
    /// Backs `Store::limiter` (`crate::invoke::on_invoke`, spec SS7.3
    /// sandbox layer 8): caps this instance's linear memory growth.
    /// Unbounded by construction (`StoreLimitsBuilder::new().build()`
    /// leaves `memory_size` unset) so every `ExecState::new` call site
    /// that never opts into [`Self::with_memory_limit_mb`] -- every test
    /// in this crate except `crate::invoke`'s own -- is unaffected.
    ///
    /// Wrapped in [`CapTrackingLimits`] (rather than a bare
    /// `wasmtime::StoreLimits`) so a denied `memory.grow`/`table.grow` is
    /// recorded via a host-only flag *before* wasmtime turns it into a
    /// trap -- `crate::invoke::trap_to_error_body` reads
    /// [`Self::memory_cap_hit`] to classify `MEMORY_LIMIT`, instead of
    /// pattern-matching the trap's formatted message (gh security review
    /// MED finding: a malicious guest can embed "memory"/"epoch" in an
    /// error string surfaced through a host-call failure to spoof or
    /// evade classification -- see `CapTrackingLimits`'s own doc).
    pub limits: CapTrackingLimits,
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
            limits: CapTrackingLimits::new(StoreLimitsBuilder::new().build()),
        }
    }

    /// Arms [`Self::limits`] with a hard `memory_limit_mb` MiB cap,
    /// `trap_on_grow_failure(true)` so a `memory.grow` beyond it raises a
    /// wasmtime trap (`"forcing trap when growing memory to N bytes"`,
    /// classified `MEMORY_LIMIT` by `crate::invoke::trap_to_error_body`)
    /// rather than `memory.grow` quietly returning `-1` to the guest or
    /// growth continuing unbounded (gh security review MED finding: this
    /// sandbox layer was previously unenforced -- only the epoch deadline
    /// was wired). `crate::invoke::on_invoke` is the sole caller; it
    /// derives `memory_limit_mb` from the loaded bundle's
    /// `limits.memory_mb`/`EXECUTOR_MEMORY_LIMIT_MB` clamped to
    /// `EXECUTOR_MAX_MEMORY_LIMIT_MB` (spec SS7.3).
    #[must_use]
    pub fn with_memory_limit_mb(mut self, memory_limit_mb: u32) -> Self {
        let bytes = (memory_limit_mb as usize).saturating_mul(1024 * 1024);
        self.limits = CapTrackingLimits::new(
            StoreLimitsBuilder::new()
                .memory_size(bytes)
                .trap_on_grow_failure(true)
                .build(),
        );
        self
    }

    /// Whether this instance's configured memory/table cap denied at
    /// least one growth request (`CapTrackingLimits::memory_growing`/
    /// `table_growing` below) -- read by `crate::invoke::on_invoke` after
    /// a call traps to drive `MEMORY_LIMIT` classification from this
    /// host-only, guest-unreachable signal rather than the trap's
    /// formatted text.
    #[must_use]
    pub fn memory_cap_hit(&self) -> bool {
        self.limits.cap_hit()
    }
}

/// Wraps `wasmtime::StoreLimits` to record, in [`Self::cap_hit`], the
/// exact moment a `memory.grow`/`table.grow` request is denied by this
/// instance's configured cap -- set only from inside
/// [`ResourceLimiter::memory_growing`]/[`ResourceLimiter::table_growing`]
/// below, i.e. only by this executor's own trusted host code reacting to
/// the numeric growth request wasmtime passes it, never from any
/// guest-supplied string. `crate::invoke::trap_to_error_body` reads this
/// flag instead of substring-matching the trap's formatted message (gh
/// security review MED finding: matching on "memory"/"epoch" text is
/// spoofable -- a guest can drive an arbitrary string into the error
/// chain, e.g. via a host-call argument a capability handler echoes back
/// on failure, letting it force or evade a `MEMORY_LIMIT`/
/// `EXECUTOR_DEADLINE` classification that has nothing to do with an
/// actual cap or deadline).
pub struct CapTrackingLimits {
    inner: StoreLimits,
    hit: Arc<AtomicBool>,
}

impl CapTrackingLimits {
    fn new(inner: StoreLimits) -> Self {
        Self {
            inner,
            hit: Arc::new(AtomicBool::new(false)),
        }
    }

    fn cap_hit(&self) -> bool {
        self.hit.load(Ordering::Relaxed)
    }
}

impl ResourceLimiter for CapTrackingLimits {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        let result = self.inner.memory_growing(current, desired, maximum);
        if result.is_err() {
            self.hit.store(true, Ordering::Relaxed);
        }
        result
    }

    fn memory_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        self.inner.memory_grow_failed(error)
    }

    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        let result = self.inner.table_growing(current, desired, maximum);
        if result.is_err() {
            self.hit.store(true, Ordering::Relaxed);
        }
        result
    }

    fn table_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        self.inner.table_grow_failed(error)
    }

    fn instances(&self) -> usize {
        self.inner.instances()
    }

    fn tables(&self) -> usize {
        self.inner.tables()
    }

    fn memories(&self) -> usize {
        self.inner.memories()
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

    /// `with_memory_limit_mb` must actually deny a `memory_growing` request
    /// once `desired` exceeds the MiB cap converted to bytes -- the exact
    /// `ResourceLimiter` call `Store::limiter` (`crate::invoke::on_invoke`)
    /// drives on every real `memory.grow`. Exercising the trait method
    /// directly (rather than through a full wasmtime instantiation, which
    /// `invoke.rs`'s own fixture-backed tests cover end to end) keeps this
    /// assertion fast and independent of the wasm fixture.
    #[test]
    fn with_memory_limit_mb_denies_growth_past_the_cap() {
        let mut state =
            ExecState::new(None, "waddles.test.app".to_string(), 1).with_memory_limit_mb(1);
        let one_mib = 1024 * 1024;
        assert!(!state.memory_cap_hit(), "no growth has been denied yet");
        assert!(state
            .limits
            .memory_growing(0, one_mib, None)
            .expect("growth to exactly the cap is a decision, not an error"));
        assert!(
            !state.memory_cap_hit(),
            "growth within the cap must not flip the flag"
        );

        state
            .limits
            .memory_growing(one_mib, one_mib + 1, None)
            .expect_err("growth past the cap must trap, not return Ok(false)");
        // The typed, guest-unreachable signal `trap_to_error_body` (see
        // `crate::invoke`) actually classifies on, in place of matching
        // the trap's formatted text (gh security review MED finding).
        assert!(
            state.memory_cap_hit(),
            "growth past the cap must flip the host-only cap_hit flag"
        );
    }

    /// `memory_grow_failed`/`table_grow_failed` fire only when a grow
    /// *permitted* by `memory_growing`/`table_growing` subsequently fails
    /// at the OS/allocator level (real allocation failure) -- a distinct,
    /// much rarer path from this instance's configured cap denying the
    /// request outright (covered above). `CapTrackingLimits` must still
    /// delegate them to the wrapped `StoreLimits` (whose own default
    /// ignores-and-logs) rather than dropping the notification, and must
    /// NOT flip `cap_hit` for them -- that flag is reserved for an actual
    /// configured-cap denial `trap_to_error_body` can trust, not an
    /// unrelated allocator failure it never decided.
    #[test]
    fn cap_tracking_limits_delegates_grow_failed_hooks_without_marking_the_flag() {
        let mut limits = CapTrackingLimits::new(StoreLimitsBuilder::new().build());

        limits
            .memory_grow_failed(wasmtime::Error::msg("simulated allocator failure"))
            .expect("default StoreLimits::memory_grow_failed ignores the notification");
        assert!(!limits.cap_hit());

        limits
            .table_grow_failed(wasmtime::Error::msg("simulated allocator failure"))
            .expect("default StoreLimits::table_grow_failed ignores the notification");
        assert!(!limits.cap_hit());
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
