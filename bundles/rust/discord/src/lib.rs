//! Discord platform overlay (bundle-MCP-tools P6b T2) on `world connector-tools`.
//!
//! The host owns every transport concern (the Gateway over the generic WS
//! transport, interactions over the generic webhook route with the Ed25519
//! check done BEFORE `on-frame`, the IDENTIFY budget, the 3-second interaction
//! deadline, secrets). This crate owns only the Discord wire spec. The
//! host-testable core lives in the plain modules below; the `component` module
//! (wasm32 only) adapts it to the generated bindings.

pub mod error;
pub mod gateway;
pub mod normalize;
pub mod perms;
pub mod receiver;
pub mod sanitize;
pub mod sender;
pub mod session;
pub mod tools;

#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

#[cfg(target_arch = "wasm32")]
mod component {
    use super::bindings::exports::waddle::bundle::tool_provider::Guest as ToolGuest;
    use super::bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
    use super::bindings::exports::waddle::connector::sender::{
        Action, Guest as SenderGuest, Header, HttpRequestTpl,
    };
    use super::bindings::waddle::bundle::http;
    use super::bindings::waddle::bundle::log;
    use super::bindings::waddle::bundle::tool::ToolError as WitToolError;
    use super::bindings::waddle::connector::types::{
        ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
    };
    use super::error::{OverlayError, Result};
    use super::perms::{GuildInfo, parse_guild, parse_member_roles};
    use super::receiver::{self, ConnCtx, Host, Level};
    use super::sender::{self, API_BASE, AUTH_HANDLE};
    use super::tools::{self, ToolFail};

    struct Component;

    /// The real host port: guarded `http` (bot token as a secret-ref, never in
    /// the component) and the scrubbed `log` import.
    struct WasmHost;

    impl WasmHost {
        /// One authenticated GET; non-2xx and every transport error are failures
        /// the caller turns into the fail-closed permission default.
        fn get(url: String) -> Result<Vec<u8>> {
            let resp = http::send(&http::Request {
                method: "GET".to_string(),
                url,
                headers: Vec::new(),
                body: None,
                secret_refs: vec![("Authorization".to_string(), AUTH_HANDLE.to_string())],
            })
            .map_err(|e| {
                OverlayError::Backend(
                    match e {
                        http::Error::Denied(_) => "http denied",
                        http::Error::Timeout => "http timeout",
                        http::Error::TooLarge(_) => "http too large",
                        http::Error::RateLimited(_) => "http rate limited",
                        http::Error::Transport(_) => "http transport",
                    }
                    .to_string(),
                )
            })?;
            if (200..300).contains(&resp.status) && !resp.truncated {
                Ok(resp.body)
            } else {
                Err(OverlayError::Backend(format!(
                    "http status {}",
                    resp.status
                )))
            }
        }
    }

    impl Host for WasmHost {
        fn guild(&self, guild_id: &str) -> Result<GuildInfo> {
            parse_guild(&Self::get(format!("{API_BASE}/guilds/{guild_id}"))?)
        }

        fn member_roles(&self, guild_id: &str, user_id: &str) -> Result<Vec<String>> {
            let body = Self::get(format!("{API_BASE}/guilds/{guild_id}/members/{user_id}"))?;
            parse_member_roles(&body, guild_id)
        }

        fn log(&self, level: Level, message: &str, fields_json: &str) {
            let lvl = match level {
                Level::Error => log::Level::Error,
                Level::Warn => log::Level::Warn,
                Level::Info => log::Level::Info,
                Level::Debug => log::Level::Debug,
            };
            log::write(lvl, message, fields_json);
        }
    }

    fn to_frame_error(e: OverlayError) -> FrameError {
        match e {
            OverlayError::Unsupported(m) => FrameError::Unsupported(m),
            OverlayError::Malformed(m) => FrameError::Malformed(m),
            OverlayError::Backend(m) => FrameError::Backend(m),
        }
    }

    fn conn(ctx: ConnectionCtx) -> ConnCtx {
        ConnCtx {
            platform: ctx.platform,
            source_id: ctx.source_id,
            connection_id: ctx.connection_id,
            session_token: ctx.session_token,
        }
    }

    impl ReceiverGuest for Component {
        fn on_connect(ctx: ConnectionCtx) -> std::result::Result<HandshakePayload, FrameError> {
            let hs = receiver::on_connect(&WasmHost, &conn(ctx)).map_err(to_frame_error)?;
            Ok(HandshakePayload {
                bytes: hs.bytes,
                secret_refs: hs.secret_refs,
            })
        }

        fn on_frame(frame: RawFrame) -> std::result::Result<Vec<NormalizedEvent>, FrameError> {
            let events = receiver::on_frame(
                &WasmHost,
                &conn(frame.ctx),
                &frame.bytes,
                &frame.received_at,
            )
            .map_err(to_frame_error)?;
            Ok(events
                .into_iter()
                .map(|e| NormalizedEvent {
                    platform: e.platform,
                    event_type: e.event_type,
                    actor: e.actor,
                    payload_json: e.payload_json,
                    occurred_at: e.occurred_at,
                })
                .collect())
        }

        fn on_heartbeat_due(
            ctx: ConnectionCtx,
        ) -> std::result::Result<Option<Vec<u8>>, FrameError> {
            receiver::on_heartbeat_due(&conn(ctx)).map_err(to_frame_error)
        }

        fn on_disconnect(_ctx: ConnectionCtx, reason: String) {
            receiver::on_disconnect(&WasmHost, &reason);
        }
    }

    impl SenderGuest for Component {
        fn build_request(action: Action) -> std::result::Result<HttpRequestTpl, FrameError> {
            let t = sender::build_request(&sender::Action {
                kind: action.kind,
                target_source_id: action.target_source_id,
                payload_json: action.payload_json,
            })
            .map_err(to_frame_error)?;
            Ok(HttpRequestTpl {
                method: t.method,
                url: t.url,
                headers: t
                    .headers
                    .into_iter()
                    .map(|(name, value)| Header { name, value })
                    .collect(),
                body: t.body,
                secret_refs: t.secret_refs,
            })
        }
    }

    impl ToolGuest for Component {
        fn call(name: String, args: Vec<u8>) -> std::result::Result<Vec<u8>, WitToolError> {
            tools::call(&name, &args).map_err(|e| match e {
                ToolFail::Unknown => WitToolError::InvalidArgs("unknown tool".to_string()),
                ToolFail::InvalidArgs(m) => WitToolError::InvalidArgs(m),
            })
        }
    }

    super::bindings::export!(Component with_types_in super::bindings);
}
