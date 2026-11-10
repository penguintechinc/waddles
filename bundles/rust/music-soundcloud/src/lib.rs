//! SoundCloud music SERVICE bundle.
//!
//! An always-on `kind: service` bundle on `world stage-tools` exporting
//! `tool-provider` with `music.soundcloud.{search,track,playlist.get,resolve}`.
//! All logic lives in [`logic`] (host-testable behind the `Upstream`/`Store`/
//! `Clock` seams); the wasm-only module below supplies the real `http`, `kv`,
//! `clock` and `log` imports. The OAuth token never enters the component: the
//! request carries a header secret-ref the host resolves and injects.

pub mod logic;

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

#[cfg(target_arch = "wasm32")]
mod component {
    use std::sync::{Arc, OnceLock};

    use super::bindings::exports::waddle::bundle::tool_provider::Guest;
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::bundle::{clock, http, kv, log};
    use super::logic::{self, Clock, HttpFail, HttpOut, Soundcloud, Store, Upstream};
    use waddle_sdk::tool::{ToolError, ToolRegistry};

    pub struct Component;

    /// Real guarded outbound GET with the credential as a header secret-ref.
    struct WasmUpstream;

    impl Upstream for WasmUpstream {
        /// Sends the GET; the host injects `Authorization` from the secret ref.
        fn get(&self, url: &str) -> Result<HttpOut, HttpFail> {
            let req = http::Request {
                method: "GET".to_string(),
                url: url.to_string(),
                headers: vec![http::Header {
                    name: "Accept".to_string(),
                    value: "application/json; charset=utf-8".to_string(),
                }],
                body: None,
                secret_refs: vec![(logic::AUTH_REF.0.to_string(), logic::AUTH_REF.1.to_string())],
            };
            match http::send(&req) {
                Ok(r) => {
                    let header = |n: &str| {
                        r.headers
                            .iter()
                            .find(|h| h.name.eq_ignore_ascii_case(n))
                            .map(|h| h.value.clone())
                    };
                    Ok(HttpOut {
                        status: r.status,
                        location: header("location"),
                        retry_after: header("retry-after").and_then(|v| v.trim().parse().ok()),
                        body: r.body,
                    })
                }
                Err(http::Error::Denied(_)) => Err(HttpFail::Denied),
                Err(http::Error::Timeout) => Err(HttpFail::Timeout),
                Err(http::Error::RateLimited(s)) => Err(HttpFail::RateLimited(s)),
                Err(http::Error::TooLarge(_)) | Err(http::Error::Transport(_)) => {
                    Err(HttpFail::Transport)
                }
            }
        }
    }

    /// The bundle's own kv (host-scoped to this community + app).
    struct WasmStore;

    impl Store for WasmStore {
        /// Raw read; errors carry no host detail.
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|_| "kv".to_string())
        }
        /// Write with TTL.
        fn set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), String> {
            kv::set(key, value, ttl_s).map_err(|_| "kv".to_string())
        }
        /// Atomic counter with TTL.
        fn incr(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String> {
            kv::increment(key, delta, ttl_s).map_err(|_| "kv".to_string())
        }
    }

    struct WasmClock;

    impl Clock for WasmClock {
        /// Stage wall clock in seconds.
        fn now_secs(&self) -> u64 {
            clock::now_millis() / 1000
        }
    }

    fn to_wit(e: ToolError) -> WitError {
        match e {
            ToolError::NotGranted => WitError::NotGranted,
            ToolError::Forbidden => WitError::Forbidden,
            ToolError::Timeout => WitError::Timeout,
            ToolError::DepthExceeded => WitError::DepthExceeded,
            ToolError::PoolExhausted => WitError::PoolExhausted,
            ToolError::TargetUnavailable => WitError::TargetUnavailable,
            ToolError::InvalidArgs(m) => WitError::InvalidArgs(m),
            ToolError::NoTransport => {
                WitError::InvalidArgs("no tool transport installed".to_string())
            }
        }
    }

    /// One PII-free line: known tool label + outcome code only.
    fn log_outcome(tool: &str, outcome: &str) {
        let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
        log::write(log::Level::Debug, "music.soundcloud tool call", &fields);
    }

    static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

    fn registry() -> &'static ToolRegistry {
        REGISTRY.get_or_init(|| {
            logic::build_registry(Arc::new(Soundcloud::new(
                Arc::new(WasmUpstream),
                Arc::new(WasmStore),
                Arc::new(WasmClock),
            )))
        })
    }

    impl Guest for Component {
        /// Routes `name` through the tool table; unknown tools are a typed error.
        fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            let result = registry().call(&name, &args);
            log_outcome(
                logic::tool_log_label(&name),
                match &result {
                    Ok(_) => "ok",
                    Err(ToolError::InvalidArgs(_)) => "invalid_args",
                    Err(ToolError::NotGranted) => "not_granted",
                    Err(ToolError::Forbidden) => "forbidden",
                    Err(ToolError::PoolExhausted) => "rate_limited",
                    Err(_) => "error",
                },
            );
            result.map_err(to_wit)
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
