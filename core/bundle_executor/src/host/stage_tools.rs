//! Host side of the `stage-tools` world's `tool` import (P3b). The guest-facing
//! `tool.invoke` is reachable once linked; routing it to the stage (the sole
//! gate/resolve authority) is a later task, so until then every call FAILS
//! CLOSED with a typed `tool-error` -- never a fabricated success.

use crate::engine::stage_tools_world::waddle::bundle::tool;
use crate::host::ExecState;

impl tool::Host for ExecState {
    /// Fail-closed `tool.invoke`: denies with `not-granted` and logs (PII-free:
    /// target/tool only, never args). Replaced by the `Db:"tool.invoke"` stage
    /// round-trip in the host-import task.
    async fn invoke(
        &mut self,
        target: String,
        tool_name: String,
        _args: Vec<u8>,
        _idempotency_key: Option<String>,
    ) -> Result<Vec<u8>, tool::ToolError> {
        tracing::warn!(target = %target, tool = %tool_name, "tool.invoke not wired to the stage; denied");
        Err(tool::ToolError::NotGranted)
    }
}
