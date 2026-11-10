//! Economy policy and tools over the host's atomic currency capability.
//! Money updates never use guest-side read/modify/write sequences.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;
#[cfg(target_arch = "wasm32")]
mod component {
    use std::sync::{Arc, OnceLock};

    use super::bindings::exports::waddle::bundle::tool_provider::Guest;
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::bundle::{economy, kv, log};
    use super::logic::{self, ConfigStore, EconomyHost};
    use waddle_sdk::tool::{ToolError, ToolRegistry};

    pub struct Component;
    struct WasmHost;

    /// Maps capability failures to stable tool errors without exposing backend details.
    fn host_err(e: economy::Error) -> ToolError {
        match e {
            // `elevated_not_granted`: the CALLING app was never approved (or was
            // revoked) for this dangerous primitive.
            economy::Error::Denied(c) if c == "not_granted" || c == "elevated_not_granted" => {
                ToolError::NotGranted
            }
            economy::Error::Denied(_) => ToolError::Forbidden,
            economy::Error::Unavailable(_) => ToolError::TargetUnavailable,
            economy::Error::Backend(_) => ToolError::TargetUnavailable,
            economy::Error::InsufficientFunds(_) => {
                ToolError::InvalidArgs("insufficient_funds".into())
            }
            economy::Error::OverCap(_) => ToolError::InvalidArgs("over_cap".into()),
            economy::Error::NotAMember => ToolError::InvalidArgs("not_a_member".into()),
            economy::Error::Invalid(m) => ToolError::InvalidArgs(m),
        }
    }

    impl EconomyHost for WasmHost {
        fn balance(&self, user: &str) -> Result<i64, ToolError> {
            economy::balance(user).map_err(host_err)
        }
        fn credit(&self, user: &str, amount: u64) -> Result<i64, ToolError> {
            economy::credit(user, amount).map_err(host_err)
        }
        fn debit(&self, user: &str, amount: u64) -> Result<i64, ToolError> {
            economy::debit(user, amount).map_err(host_err)
        }
        fn transfer(&self, from: &str, to: &str, amount: u64) -> Result<(), ToolError> {
            economy::transfer(from, to, amount).map_err(host_err)
        }
        fn transfer_from(
            &self,
            from: &str,
            to: &str,
            amount: u64,
            reason: &str,
        ) -> Result<(i64, i64), ToolError> {
            economy::transfer_from(from, to, amount, reason)
                .map(|r| (r.from_balance, r.to_balance))
                .map_err(host_err)
        }
        fn admin_set(&self, user: &str, value: u64) -> Result<i64, ToolError> {
            economy::admin_adjust(user, economy::BalanceAdjust::Set(value)).map_err(host_err)
        }
        fn admin_delta(&self, user: &str, delta: i64) -> Result<i64, ToolError> {
            economy::admin_adjust(user, economy::BalanceAdjust::Delta(delta)).map_err(host_err)
        }
        fn pay_pot(&self, winners: &[(String, u64)]) -> Result<(), ToolError> {
            let rows = winners
                .iter()
                .map(|(user, amount)| economy::CreditEntry {
                    user: user.clone(),
                    amount: *amount,
                })
                .collect::<Vec<_>>();
            economy::pay_pot(&rows).map_err(host_err)
        }
        fn exchange(&self, user: &str, amount: u64, num: u32, den: u32) -> Result<i64, ToolError> {
            economy::exchange(user, amount, num, den).map_err(host_err)
        }
        fn leaderboard(&self, limit: u32) -> Result<Vec<(String, i64)>, ToolError> {
            economy::leaderboard(limit)
                .map(|rows| rows.into_iter().map(|r| (r.user, r.balance)).collect())
                .map_err(host_err)
        }
    }
    /// The bundle's own kv (host-scoped to this community + app).
    struct WasmKv;

    impl ConfigStore for WasmKv {
        /// Raw kv read; the error never carries host detail.
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|_| "kv unavailable".to_string())
        }
    }

    /// Maps the SDK tool error onto the WIT one.
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

    /// Emits one PII-free line: tool name and outcome code only (never args).
    fn log_outcome(tool: &str, outcome: &str) {
        let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
        log::write(log::Level::Debug, "economy tool call", &fields);
    }

    static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

    fn registry() -> &'static ToolRegistry {
        REGISTRY.get_or_init(|| logic::build_registry(Arc::new(WasmHost), Arc::new(WasmKv)))
    }

    impl Guest for Component {
        /// Routes `name` through the tool table; unknown tools are a typed error.
        fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            let result = registry().call(&name, &args);
            log_outcome(
                &name,
                match &result {
                    Ok(_) => "ok",
                    Err(ToolError::InvalidArgs(_)) => "invalid_args",
                    Err(ToolError::NotGranted) => "not_granted",
                    Err(ToolError::Forbidden) => "forbidden",
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
