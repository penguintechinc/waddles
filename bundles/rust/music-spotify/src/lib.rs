//! Spotify music SERVICE bundle.
//!
//! An always-on `kind: service` bundle on `world stage-tools`: it exports
//! `tool-provider` and reaches Spotify only through the host `http` capability
//! with secret-refs, so no token ever enters the guest. All tool logic lives in
//! [`logic`] (host-testable behind three seams); this file only supplies the
//! real wasm implementations of those seams and routes `tool-provider.call`.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;

#[cfg(target_arch = "wasm32")]
mod component {
    use std::sync::{Arc, OnceLock};

    use super::bindings::exports::waddle::bundle::tool_provider::Guest;
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::bundle::{clock, http, kv, log};
    use super::logic::{self, Clock, Http, HttpError, HttpRequest, HttpResponse, Spotify, Store};
    use waddle_sdk::tool::{ToolError, ToolRegistry};

    struct Component;

    /// Real host `http` capability: credentials ride as secret-refs only.
    struct WasmHttp;

    impl Http for WasmHttp {
        /// Sends through the host guard; the `Authorization` value is injected host-side.
        fn send(&self, req: &HttpRequest) -> Result<HttpResponse, HttpError> {
            let wit = http::Request {
                method: req.method.to_string(),
                url: req.url.clone(),
                headers: req
                    .headers
                    .iter()
                    .map(|(n, v)| http::Header {
                        name: n.clone(),
                        value: v.clone(),
                    })
                    .collect(),
                body: req.body.clone(),
                secret_refs: vec![(
                    "Authorization".to_string(),
                    req.cred.secret_ref().to_string(),
                )],
            };
            match http::send(&wit) {
                Ok(r) => Ok(HttpResponse {
                    status: r.status,
                    headers: r.headers.into_iter().map(|h| (h.name, h.value)).collect(),
                    body: r.body,
                    truncated: r.truncated,
                }),
                Err(http::Error::Denied(m)) => Err(HttpError::Denied(m)),
                Err(http::Error::Timeout) => Err(HttpError::Timeout),
                Err(http::Error::TooLarge(n)) => Err(HttpError::TooLarge(n)),
                Err(http::Error::RateLimited(s)) => Err(HttpError::RateLimited(s)),
                Err(http::Error::Transport(m)) => Err(HttpError::Transport(m)),
            }
        }
    }

    /// The bundle's own kv (host-scoped to this community + app).
    struct WasmKv;

    impl Store for WasmKv {
        /// Raw kv read; the error never carries host detail.
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|_| "kv unavailable".to_string())
        }
        /// Raw kv write with TTL.
        fn set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), String> {
            kv::set(key, value, ttl_s).map_err(|_| "kv unavailable".to_string())
        }
        /// Atomic counter bump with TTL.
        fn increment(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String> {
            kv::increment(key, delta, ttl_s).map_err(|_| "kv unavailable".to_string())
        }
    }

    /// Host wall clock.
    struct WasmClock;

    impl Clock for WasmClock {
        fn now_millis(&self) -> u64 {
            clock::now_millis()
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

    /// One PII-free line: a known tool label and an outcome code, never args.
    fn log_outcome(tool: &str, outcome: &str) {
        let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
        log::write(log::Level::Debug, "music-spotify tool call", &fields);
    }

    static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

    fn registry() -> &'static ToolRegistry {
        REGISTRY.get_or_init(|| {
            logic::build_registry(Arc::new(Spotify::new(
                Arc::new(WasmHttp),
                Arc::new(WasmKv),
                Arc::new(WasmClock),
            )))
        })
    }

    impl Guest for Component {
        /// Routes `name` through the tool table; unknown tools are a typed error.
        fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            let result = registry().call(&name, &args);
            let outcome = match &result {
                Ok(_) => "ok".to_string(),
                Err(e) => logic::outcome_code(e),
            };
            log_outcome(logic::tool_log_label(&name), &outcome);
            result.map_err(to_wit)
        }
    }

    super::bindings::export!(Component with_types_in super::bindings);
}
