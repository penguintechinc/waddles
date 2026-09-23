#[allow(warnings)]
mod bindings;

use bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
use bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
use bindings::waddle::bundle::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};
use bindings::waddle::bundle::{clock, context, db, flags, kv, log, relay};

struct Component;

impl ProcessGuest for Component {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let out = match event.event_type.as_str() {
            "get-context" => {
                let ctx = context::get_context();
                format!(
                    "{{\"tenant\":\"{}\",\"app_id\":\"{}\",\"message_id\":\"{}\"}}",
                    ctx.tenant, ctx.app_id, ctx.message_id
                )
            }
            "kv-roundtrip" => {
                kv::set("probe", b"hostile", 0).ok();
                let got = kv::get("probe").ok().flatten();
                format!("{:?}", got.map(|v| String::from_utf8_lossy(&v).into_owned()))
            }
            "db-roundtrip" => {
                let result = db::execute("SELECT 1", &[]);
                format!("{:?}", result.is_ok())
            }
            "log-write" => {
                log::write(log::Level::Debug, "hostile fixture log probe", "{}");
                "logged".to_string()
            }
            "clock-read" => format!("{}", clock::now_millis()),
            "socket-probe" => {
                // Negative sandbox test #1 (spec Sec14.6): the guest attempts
                // a real TCP connect. Under wasm32-wasip2 this routes through
                // wasi:sockets, which the executor's native denial config
                // (allow_tcp(false)) refuses before any socket() syscall.
                // The guest must see a clean `io::Error`, never a trap/panic.
                match std::net::TcpStream::connect("127.0.0.1:1") {
                    Ok(_) => "socket-unexpectedly-succeeded".to_string(),
                    Err(e) => format!("socket-denied:{}", e.kind() as i32),
                }
            }
            _ => return Err(UnsupportedStage {
                stage: "process".to_string(),
            }),
        };
        Ok(Some(PlatformEvent {
            payload_json: out,
            ..event
        }))
    }
}

impl ActionGuest for Component {
    fn dispatch(_envelope: StageEnvelope, _config: String) -> Result<TransportResult, TransportError> {
        let tier = flags::tier();
        let enabled = flags::enabled("waddles.hostile-fixture", false);
        relay::push("twitch", "{\"probe\":true}").ok();
        Ok(TransportResult {
            ok: enabled,
            status: Some(200),
            detail: Some(tier),
            provider_message_id: None,
        })
    }
}

bindings::export!(Component with_types_in bindings);
