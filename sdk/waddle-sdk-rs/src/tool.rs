//! Bundle-to-bundle tool invocation: a client helper ([`invoke`]) and a
//! service-side registry ([`ToolRegistry`] / [`register_tool`]) backing the
//! `tool-provider.call` export (`wit/waddle-bundle/tool.wit`, `world
//! stage-tools`).
//!
//! Wire contract (pinned): args and results are canonical JSON bytes in the
//! WIT `list<u8>`. The host import already decodes the stage's `{ok}` /
//! `{tool_error}` envelope into the WIT `result<list<u8>, tool-error>`, so the
//! client sees `Result<Vec<u8>, ToolError>` directly.
//!
//! The `stage-tools` bindings live in the bundle crate (this crate's
//! `bindings_glue` generates only the `stage` world, which must stay frozen),
//! so the host import is reached through the [`ToolTransport`] seam: a bundle
//! installs a one-line adapter over its generated `tool::invoke` with
//! [`install_transport`], and its generated `tool-provider::Guest::call` calls
//! [`dispatch`]. Everything here is host-testable.
//!
//! Idempotency: `tool-provider.call` has no key parameter, so the stage merges
//! the caller's key into the forwarded args under [`IDEMPOTENCY_ARG`]; the
//! registry strips it and hands it to the handler via [`ToolContext`].

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use thiserror::Error;

/// Reserved top-level args field carrying the idempotency key (must match
/// `bundle_tool_call::IDEMPOTENCY_ARG` and the Python SDK).
pub const IDEMPOTENCY_ARG: &str = "__idempotency_key";

/// Reserved top-level args field carrying the HOST-VERIFIED actor + scopes
/// (`{"actor": <uuid|null>, "scopes": [..]}`; must match
/// `bundle_tool_call::CTX_ARG` and the Python SDK). The host overwrites any
/// caller-supplied value; the registry strips it and exposes it read-only via
/// [`ToolContext`]. A bundle must never read an actor/scope from its own args.
pub const CTX_ARG: &str = "__ctx";

/// Mirrors WIT `tool.tool-error`, plus [`ToolError::NoTransport`] (SDK-local:
/// the bundle never installed a transport -- fail loud, never a silent no-op).
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ToolError {
    #[error("not granted")]
    NotGranted,
    #[error("forbidden")]
    Forbidden,
    #[error("timeout")]
    Timeout,
    #[error("depth exceeded")]
    DepthExceeded,
    #[error("pool exhausted")]
    PoolExhausted,
    #[error("target unavailable")]
    TargetUnavailable,
    #[error("invalid args: {0}")]
    InvalidArgs(String),
    #[error("no tool transport installed")]
    NoTransport,
}

/// The raw host import: `tool.invoke(target, tool, args, idempotency-key)`.
pub trait ToolTransport: Send + Sync {
    /// Calls the host import with canonical-JSON `args` bytes.
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &[u8],
        idempotency_key: Option<&str>,
    ) -> Result<Vec<u8>, ToolError>;
}

static TRANSPORT: OnceLock<Box<dyn ToolTransport>> = OnceLock::new();

/// Installs the process-wide transport (once; a second install is ignored and
/// returns `false`).
pub fn install_transport(t: Box<dyn ToolTransport>) -> bool {
    TRANSPORT.set(t).is_ok()
}

/// Invokes `tool` on `target` through `transport`, serializing `args` to JSON.
pub fn invoke_with<A: Serialize>(
    transport: &dyn ToolTransport,
    target: &str,
    tool: &str,
    args: &A,
    idempotency_key: Option<String>,
) -> Result<Vec<u8>, ToolError> {
    let bytes = serde_json::to_vec(args).map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
    transport.invoke(target, tool, &bytes, idempotency_key.as_deref())
}

/// Client helper: invokes `tool` on `target` via the installed transport.
/// Writes must pass an `idempotency_key`. Returns the result's JSON bytes.
pub fn invoke<A: Serialize>(
    target: &str,
    tool: &str,
    args: &A,
    idempotency_key: Option<String>,
) -> Result<Vec<u8>, ToolError> {
    let t = TRANSPORT.get().ok_or(ToolError::NoTransport)?;
    invoke_with(t.as_ref(), target, tool, args, idempotency_key)
}

/// [`invoke`] plus decoding the result JSON into `R`.
pub fn invoke_json<A: Serialize, R: DeserializeOwned>(
    target: &str,
    tool: &str,
    args: &A,
    idempotency_key: Option<String>,
) -> Result<R, ToolError> {
    let bytes = invoke(target, tool, args, idempotency_key)?;
    serde_json::from_slice(&bytes).map_err(|e| ToolError::InvalidArgs(e.to_string()))
}

/// Per-call context handed to a tool handler.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ToolContext {
    /// The caller's idempotency key (stripped from the args), if supplied.
    pub idempotency_key: Option<String>,
    /// The host-VERIFIED actor (community `user_uuid`) behind this call, or
    /// `None` when the call is not user-originated / the host resolved no
    /// actor. Host-asserted, never caller-suppliable.
    pub actor: Option<String>,
    /// The host-derived scopes of that actor (from their community role).
    /// Empty when `actor` is `None`.
    pub actor_scopes: Vec<String>,
}

impl ToolContext {
    /// `true` only when a verified actor is present AND holds `scope`.
    /// (Scope-restricted tools are already refused host-side before dispatch;
    /// this is for finer, in-tool decisions on verified data.)
    pub fn has_scope(&self, scope: &str) -> bool {
        self.actor.is_some() && self.actor_scopes.iter().any(|s| s == scope)
    }
}

type Handler = Arc<dyn Fn(&ToolContext, Value) -> Result<Value, ToolError> + Send + Sync>;

/// Name -> handler routing for a service bundle's `tool-provider.call`.
#[derive(Default)]
pub struct ToolRegistry {
    handlers: HashMap<String, Handler>,
}

impl ToolRegistry {
    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers (or replaces) the handler for `name`.
    pub fn register<F>(&mut self, name: &str, handler: F)
    where
        F: Fn(&ToolContext, Value) -> Result<Value, ToolError> + Send + Sync + 'static,
    {
        self.handlers.insert(name.to_string(), Arc::new(handler));
    }

    /// `tool-provider.call`: parses the JSON `args`, extracts
    /// [`IDEMPOTENCY_ARG`] into the [`ToolContext`], runs the handler on the
    /// remaining args and encodes its result. Unknown tool -> `invalid-args`
    /// (the WIT variant has no dedicated case).
    pub fn call(&self, name: &str, args: &[u8]) -> Result<Vec<u8>, ToolError> {
        let handler = self
            .handlers
            .get(name)
            .ok_or_else(|| ToolError::InvalidArgs(format!("unknown tool: {name}")))?;
        let mut value: Value = serde_json::from_slice(args)
            .map_err(|e| ToolError::InvalidArgs(format!("args are not JSON: {e}")))?;
        let mut ctx = ToolContext::default();
        if let Some(obj) = value.as_object_mut() {
            match obj.remove(IDEMPOTENCY_ARG) {
                None | Some(Value::Null) => {}
                Some(Value::String(k)) => ctx.idempotency_key = Some(k),
                Some(_) => {
                    return Err(ToolError::InvalidArgs(
                        "idempotency key must be a string".to_string(),
                    ));
                }
            }
            match obj.remove(CTX_ARG) {
                None | Some(Value::Null) => {}
                Some(Value::Object(c)) => {
                    // Host-written; any malformed shape is a host bug -> fail
                    // loud, never a half-trusted context.
                    let actor = match c.get("actor") {
                        None | Some(Value::Null) => None,
                        Some(Value::String(a)) => Some(a.clone()),
                        Some(_) => return Err(bad_ctx()),
                    };
                    let scopes = match c.get("scopes") {
                        None | Some(Value::Null) => Vec::new(),
                        Some(Value::Array(a)) => a
                            .iter()
                            .map(|v| v.as_str().map(str::to_string).ok_or_else(bad_ctx))
                            .collect::<Result<Vec<_>, _>>()?,
                        Some(_) => return Err(bad_ctx()),
                    };
                    // Scopes without an actor are meaningless: drop them.
                    if actor.is_some() {
                        ctx.actor = actor;
                        ctx.actor_scopes = scopes;
                    }
                }
                Some(_) => return Err(bad_ctx()),
            }
        }
        let out = handler(&ctx, value)?;
        serde_json::to_vec(&out).map_err(|e| ToolError::InvalidArgs(e.to_string()))
    }
}

fn bad_ctx() -> ToolError {
    ToolError::InvalidArgs("malformed host context".to_string())
}

static GLOBAL: OnceLock<Mutex<ToolRegistry>> = OnceLock::new();

fn global() -> &'static Mutex<ToolRegistry> {
    GLOBAL.get_or_init(|| Mutex::new(ToolRegistry::new()))
}

/// Locks the process-wide registry, recovering from poison LOUDLY: a
/// poisoned lock only means a thread panicked while holding it (the map is
/// still structurally valid), but the panic is worth a log line.
fn lock_global() -> std::sync::MutexGuard<'static, ToolRegistry> {
    match global().lock() {
        Ok(guard) => guard,
        Err(poisoned) => {
            // A logging failure must never break tool dispatch. The host
            // log import only exists inside the wasm component.
            #[cfg(target_arch = "wasm32")]
            let _ = crate::log::write(
                crate::log::Level::Warn,
                "tool registry lock was poisoned by a panicking thread; recovering",
                &crate::log::Fields::new(),
            );
            poisoned.into_inner()
        }
    }
}

/// Registers `handler` for `name` in the process-wide registry.
pub fn register_tool<F>(name: &str, handler: F)
where
    F: Fn(&ToolContext, Value) -> Result<Value, ToolError> + Send + Sync + 'static,
{
    // A poisoned lock only means a registering thread panicked; the map is
    // still structurally valid, so recover it.
    let mut g = lock_global();
    g.register(name, handler);
}

/// `tool-provider.call` entry point over the process-wide registry.
pub fn dispatch(name: &str, args: &[u8]) -> Result<Vec<u8>, ToolError> {
    // Clone-free: the handler runs under the lock only for the lookup via
    // `call`; handlers are sync and short, and must not call `register_tool`.
    let g = lock_global();
    g.call(name, args)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex as StdMutex;

    type Seen = (String, String, Vec<u8>, Option<String>);

    struct Fake {
        seen: StdMutex<Vec<Seen>>,
        reply: Result<Vec<u8>, ToolError>,
    }
    impl ToolTransport for Fake {
        fn invoke(
            &self,
            t: &str,
            n: &str,
            a: &[u8],
            k: Option<&str>,
        ) -> Result<Vec<u8>, ToolError> {
            self.seen
                .lock()
                .unwrap()
                .push((t.into(), n.into(), a.to_vec(), k.map(String::from)));
            self.reply.clone()
        }
    }

    #[test]
    fn client_encodes_args_as_json_and_passes_key() {
        let f = Fake {
            seen: StdMutex::default(),
            reply: Ok(br#"{"n":2}"#.to_vec()),
        };
        let out = invoke_with(
            &f,
            "waddles.core.economy",
            "adjust",
            &json!({"x": 1}),
            Some("k-1".into()),
        )
        .unwrap();
        assert_eq!(out, br#"{"n":2}"#);
        let seen = f.seen.lock().unwrap();
        assert_eq!(seen[0].0, "waddles.core.economy");
        assert_eq!(seen[0].1, "adjust");
        assert_eq!(
            serde_json::from_slice::<Value>(&seen[0].2).unwrap(),
            json!({"x": 1})
        );
        assert_eq!(seen[0].3.as_deref(), Some("k-1"));
    }

    #[test]
    fn client_absent_key_and_typed_error_pass_through() {
        let f = Fake {
            seen: StdMutex::default(),
            reply: Err(ToolError::Forbidden),
        };
        let err = invoke_with(&f, "t", "x", &json!({}), None).unwrap_err();
        assert_eq!(err, ToolError::Forbidden);
        assert_eq!(f.seen.lock().unwrap()[0].3, None);
    }

    #[test]
    fn invoke_without_transport_fails_loudly() {
        // No test installs a transport in this process.
        assert_eq!(
            invoke("t", "x", &json!({}), None).unwrap_err(),
            ToolError::NoTransport
        );
    }

    fn registry() -> ToolRegistry {
        let mut r = ToolRegistry::new();
        r.register("echo", |ctx, args| {
            Ok(json!({"args": args, "key": ctx.idempotency_key}))
        });
        r
    }

    #[test]
    fn registry_routes_call_to_the_handler() {
        let out = registry().call("echo", br#"{"a":1}"#).unwrap();
        let v: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(v, json!({"args": {"a": 1}, "key": null}));
    }

    #[test]
    fn registry_extracts_idempotency_key_and_strips_it() {
        let out = registry()
            .call("echo", br#"{"a":1,"__idempotency_key":"k-9"}"#)
            .unwrap();
        let v: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(v, json!({"args": {"a": 1}, "key": "k-9"}));
    }

    fn ctx_registry() -> ToolRegistry {
        let mut r = ToolRegistry::new();
        r.register("who", |ctx, args| {
            Ok(
                json!({"args": args, "actor": ctx.actor, "scopes": ctx.actor_scopes,
                      "admin": ctx.has_scope("community:admin")}),
            )
        });
        r
    }

    #[test]
    fn registry_exposes_the_verified_actor_and_strips_the_reserved_field() {
        let out = ctx_registry()
            .call(
                "who",
                br#"{"a":1,"__ctx":{"actor":"u-1","scopes":["community:admin"]}}"#,
            )
            .unwrap();
        let v: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(
            v,
            json!({"args": {"a": 1}, "actor": "u-1", "scopes": ["community:admin"], "admin": true})
        );
    }

    #[test]
    fn absent_ctx_means_no_actor_and_no_scopes() {
        let out = ctx_registry().call("who", br#"{"a":1}"#).unwrap();
        let v: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(v["actor"], Value::Null);
        assert_eq!(v["scopes"], json!([]));
        assert_eq!(v["admin"], false);
    }

    #[test]
    fn scopes_without_an_actor_are_dropped_and_malformed_ctx_fails_loud() {
        let out = ctx_registry()
            .call(
                "who",
                br#"{"__ctx":{"actor":null,"scopes":["community:admin"]}}"#,
            )
            .unwrap();
        let v: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(v["scopes"], json!([]));
        assert_eq!(v["admin"], false);
        for bad in [
            br#"{"__ctx":5}"#.as_slice(),
            br#"{"__ctx":{"actor":5}}"#,
            br#"{"__ctx":{"actor":"u","scopes":[1]}}"#,
            br#"{"__ctx":{"actor":"u","scopes":"x"}}"#,
        ] {
            assert!(matches!(
                ctx_registry().call("who", bad),
                Err(ToolError::InvalidArgs(_))
            ));
        }
    }

    #[test]
    fn registry_rejects_unknown_tool_bad_json_and_non_string_key() {
        let r = registry();
        assert!(
            matches!(r.call("nope", b"{}"), Err(ToolError::InvalidArgs(m)) if m.contains("unknown tool"))
        );
        assert!(matches!(
            r.call("echo", b"not json"),
            Err(ToolError::InvalidArgs(_))
        ));
        assert!(matches!(
            r.call("echo", br#"{"__idempotency_key":5}"#),
            Err(ToolError::InvalidArgs(_))
        ));
    }

    #[test]
    fn handler_error_propagates() {
        let mut r = ToolRegistry::new();
        r.register("deny", |_, _| Err(ToolError::Forbidden));
        assert_eq!(r.call("deny", b"{}").unwrap_err(), ToolError::Forbidden);
    }

    #[test]
    fn global_register_and_dispatch() {
        register_tool("g_echo", |_, a| Ok(a));
        assert_eq!(dispatch("g_echo", br#"{"z":1}"#).unwrap(), br#"{"z":1}"#);
    }
}
