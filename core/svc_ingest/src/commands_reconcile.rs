//! Slash-command registration: desired-state verification and the platform reconciler.
//!
//! `commands.register.guild` is a bulk overwrite of the WHOLE guild's application-command
//! set, which every community and bundle in that guild shares. It is therefore NOT a
//! bundle-callable relay op (see `bundle_capability_gate::platform_ops`); only this
//! host reconciler, acting as the platform principal, produces it, and the executor's
//! duty gate independently re-checks the submitted payload against hub-api's desired state
//! (`verify_submitted`), so even a forged queue entry cannot register anything else.
//!
//! The contract is `GET /api/v1/internal/dataplane/app-commands/desired`: the union of the
//! webhook-type commands of every community paired to the guild plus the built-in `context`
//! command, with an idempotency `version`. Every overwrite is audit-logged (guild, version,
//! count; never a name or description).

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use serde_json::Value;

use crate::discord_rest::is_snowflake;

/// Machine-JWT scope svc-ingest holds for the registry endpoints.
pub const APP_COMMANDS_JWT_SCOPE: &str = "dataplane:app-commands";
/// Hard cap (Discord): more is refused, never truncated host-side.
pub const MAX_COMMANDS: usize = 100;
/// Bound on each hub-api call.
const CALL_TIMEOUT: Duration = Duration::from_secs(5);

/// One command of the desired set (the registered payload: name + description only).
#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
pub struct DesiredCommand {
    /// Command name.
    pub name: String,
    /// Command description.
    pub description: String,
}

/// The desired registration of one guild.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Desired {
    /// Idempotency version (sha256 over exactly the registered payload).
    pub version: String,
    /// `false` = the `if_version` matched; `commands` is empty and nothing is owed.
    pub changed: bool,
    /// The desired set, in registry order (built-in first).
    pub commands: Vec<DesiredCommand>,
}

/// Why a desired-state lookup or verification failed (never carries command text).
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum RegistryError {
    /// hub-api unreachable / non-2xx / token failure; retryable.
    #[error("desired state unavailable")]
    Unavailable,
    /// The response is not the documented shape.
    #[error("desired state malformed")]
    Malformed,
    /// The submitted payload is not exactly the desired set.
    #[error("submitted commands do not match the desired state")]
    Mismatch,
}

/// Parses the desired-state response.
///
/// # Errors
/// [`RegistryError::Malformed`] for a body that is not the documented shape.
pub fn parse_desired(body: &[u8]) -> Result<Desired, RegistryError> {
    #[derive(Deserialize)]
    struct Wire {
        version: String,
        changed: bool,
        #[serde(default)]
        commands: Vec<DesiredCommand>,
    }
    let w: Wire = serde_json::from_slice(body).map_err(|_| RegistryError::Malformed)?;
    if w.version.is_empty() || w.version.len() > 128 || w.commands.len() > MAX_COMMANDS {
        return Err(RegistryError::Malformed);
    }
    Ok(Desired {
        version: w.version,
        changed: w.changed,
        commands: w.commands,
    })
}

/// The canonical payload text the reconciler submits and the gate expects: a JSON array of
/// `{name, description}` in registry order.
#[must_use]
pub fn canonical_text(commands: &[DesiredCommand]) -> String {
    let arr: Vec<Value> = commands
        .iter()
        .map(|c| serde_json::json!({ "name": c.name, "description": c.description }))
        .collect();
    Value::Array(arr).to_string()
}

/// Verifies `submitted` (the op's text) is EXACTLY `desired` (same commands, same order, no
/// extra keys). A `changed: false` desired carries no list and can never authorize a write.
///
/// # Errors
/// [`RegistryError::Mismatch`] for anything else.
pub fn verify_submitted(submitted: &str, desired: &Desired) -> Result<(), RegistryError> {
    if !desired.changed {
        // An unchanged answer means nothing is owed; a write against it is not authorized.
        // Callers fetch WITHOUT `if_version` for verification, so this is a hub contract fault.
        return Err(RegistryError::Mismatch);
    }
    let v: Value = serde_json::from_str(submitted).map_err(|_| RegistryError::Mismatch)?;
    let Value::Array(items) = v else {
        return Err(RegistryError::Mismatch);
    };
    if items.len() != desired.commands.len() {
        return Err(RegistryError::Mismatch);
    }
    for (item, want) in items.iter().zip(&desired.commands) {
        let Value::Object(o) = item else {
            return Err(RegistryError::Mismatch);
        };
        if o.len() != 2
            || o.get("name").and_then(Value::as_str) != Some(want.name.as_str())
            || o.get("description").and_then(Value::as_str) != Some(want.description.as_str())
        {
            return Err(RegistryError::Mismatch);
        }
    }
    Ok(())
}

/// Boxed future (the lookups are held as trait objects by the duty hook).
pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Source of the desired state of one guild.
pub trait DesiredLookup: Send + Sync {
    /// Desired state of `guild_id`; `if_version` asks hub-api to answer `changed: false`
    /// when it still matches (the reconciler's cheap poll). Verification passes `None`.
    fn desired<'a>(
        &'a self,
        guild_id: &'a str,
        if_version: Option<&'a str>,
    ) -> BoxFut<'a, Result<Desired, RegistryError>>;
}

/// Production [`DesiredLookup`]: hub-api over HTTP with a machine JWT.
pub struct HubDesiredLookup {
    http: reqwest::Client,
    base: String,
    jwt: Arc<service_auth::MachineJwtClient>,
}

impl HubDesiredLookup {
    /// `base` is the hub-api root (e.g. `https://hub-api:8000`).
    ///
    /// # Errors
    /// The `reqwest` client builder error.
    pub fn new(
        base: &str,
        jwt: Arc<service_auth::MachineJwtClient>,
    ) -> Result<Self, reqwest::Error> {
        let http = reqwest::Client::builder().timeout(CALL_TIMEOUT).build()?;
        Ok(Self {
            http,
            base: base.trim_end_matches('/').to_string(),
            jwt,
        })
    }
}

impl DesiredLookup for HubDesiredLookup {
    fn desired<'a>(
        &'a self,
        guild_id: &'a str,
        if_version: Option<&'a str>,
    ) -> BoxFut<'a, Result<Desired, RegistryError>> {
        Box::pin(async move {
            if !is_snowflake(guild_id) {
                return Err(RegistryError::Malformed);
            }
            let token = self
                .jwt
                .get_token()
                .await
                .map_err(|_| RegistryError::Unavailable)?;
            let mut req = self
                .http
                .get(format!(
                    "{}/api/v1/internal/dataplane/app-commands/desired",
                    self.base
                ))
                .query(&[("platform", "discord"), ("source_id", guild_id)])
                .bearer_auth(&*token);
            if let Some(v) = if_version {
                req = req.query(&[("if_version", v)]);
            }
            let resp = req.send().await.map_err(|_| RegistryError::Unavailable)?;
            if !resp.status().is_success() {
                return Err(RegistryError::Unavailable);
            }
            let body = resp.bytes().await.map_err(|_| RegistryError::Unavailable)?;
            parse_desired(&body)
        })
    }
}

/// Result of one reconcile of one guild.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Reconciled {
    /// Registered version already current: no Discord write.
    Unchanged,
    /// The guild's commands were overwritten.
    Overwritten {
        /// The version now registered (remember it as the next `if_version`).
        version: String,
        /// How many commands were registered.
        count: usize,
    },
}

/// Submits the op to the executor as the platform principal.
pub trait CommandOpSubmit: Send + Sync {
    /// Runs `commands.register.guild` for `guild_id` with `text` as the payload.
    fn submit<'a>(&'a self, guild_id: &'a str, text: String) -> BoxFut<'a, Result<(), String>>;
}

/// Reconciles one guild: poll with `if_version`, and ONLY when changed submit the canonical
/// payload built from the fetched desired state. The executor's gate re-verifies it.
///
/// # Errors
/// [`RegistryError`] for lookup failures; [`RegistryError::Unavailable`] when the submit
/// is refused or fails (retried on the next tick; the version is not advanced).
pub async fn reconcile_guild(
    lookup: &dyn DesiredLookup,
    submit: &dyn CommandOpSubmit,
    guild_id: &str,
    registered_version: Option<&str>,
) -> Result<Reconciled, RegistryError> {
    let polled = lookup.desired(guild_id, registered_version).await?;
    if !polled.changed || registered_version == Some(polled.version.as_str()) {
        return Ok(Reconciled::Unchanged);
    }
    // Registered state differs: submit exactly this set.
    let text = canonical_text(&polled.commands);
    let count = polled.commands.len();
    submit
        .submit(guild_id, text)
        .await
        .map_err(|_| RegistryError::Unavailable)?;
    tracing::info!(
        target: "audit",
        platform = "discord",
        guild_id,
        version = %polled.version,
        count,
        "slash commands overwritten"
    );
    Ok(Reconciled::Overwritten {
        version: polled.version,
        count,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    fn cmd(n: &str, d: &str) -> DesiredCommand {
        DesiredCommand {
            name: n.into(),
            description: d.into(),
        }
    }

    fn desired(version: &str, cmds: &[DesiredCommand]) -> Desired {
        Desired {
            version: version.into(),
            changed: true,
            commands: cmds.to_vec(),
        }
    }

    #[test]
    fn parses_the_hub_response_and_ignores_context_fields() {
        let body = json!({"success":true,"version":"abc","changed":true,"truncated":false,
            "commands":[{"name":"context","description":"Switch","builtin":true,"appPublicIds":[]}]});
        let d = parse_desired(body.to_string().as_bytes()).unwrap();
        assert_eq!(d.commands, vec![cmd("context", "Switch")]);
        let unchanged = json!({"version":"abc","changed":false,"commands":[]});
        assert!(
            !parse_desired(unchanged.to_string().as_bytes())
                .unwrap()
                .changed
        );
        for bad in [
            json!({"changed":true}),
            json!({"version":"","changed":true}),
            json!([]),
        ] {
            assert_eq!(
                parse_desired(bad.to_string().as_bytes()),
                Err(RegistryError::Malformed)
            );
        }
        assert_eq!(parse_desired(b"x"), Err(RegistryError::Malformed));
    }

    #[test]
    fn canonical_text_verifies_and_anything_else_is_a_mismatch() {
        let set = [cmd("context", "Switch"), cmd("ping", "Ping")];
        let d = desired("v", &set);
        verify_submitted(&canonical_text(&set), &d).unwrap();
        let bad = [
            canonical_text(&set[..1]),
            canonical_text(&[set[1].clone(), set[0].clone()]),
            canonical_text(&[set[0].clone(), cmd("ping", "Pong")]),
            canonical_text(&[set[0].clone(), cmd("ping", "Ping"), cmd("evil", "x")]),
            "[]".into(),
            json!([{"name":"context","description":"Switch","default_member_permissions":"0"},
                   {"name":"ping","description":"Ping"}])
            .to_string(),
            json!({"commands": []}).to_string(),
            "nope".into(),
        ];
        for b in bad {
            assert_eq!(
                verify_submitted(&b, &d),
                Err(RegistryError::Mismatch),
                "{b}"
            );
        }
        // An unchanged answer authorizes nothing, even an exact match.
        let mut unchanged = d.clone();
        unchanged.changed = false;
        assert_eq!(
            verify_submitted(&canonical_text(&set), &unchanged),
            Err(RegistryError::Mismatch)
        );
    }

    struct Lookup {
        answer: Desired,
        asked: Mutex<Vec<Option<String>>>,
    }
    impl DesiredLookup for Lookup {
        fn desired<'a>(
            &'a self,
            _g: &'a str,
            v: Option<&'a str>,
        ) -> BoxFut<'a, Result<Desired, RegistryError>> {
            self.asked.lock().unwrap().push(v.map(str::to_string));
            Box::pin(async move { Ok(self.answer.clone()) })
        }
    }
    struct Submit(Mutex<Vec<(String, String)>>);
    impl CommandOpSubmit for Submit {
        fn submit<'a>(&'a self, g: &'a str, t: String) -> BoxFut<'a, Result<(), String>> {
            self.0.lock().unwrap().push((g.to_string(), t));
            Box::pin(async { Ok(()) })
        }
    }

    #[tokio::test]
    async fn reconciler_writes_only_when_changed_and_submits_the_exact_desired_set() {
        let set = [cmd("context", "Switch"), cmd("ping", "Ping")];
        let lookup = Lookup {
            answer: desired("v2", &set),
            asked: Mutex::new(vec![]),
        };
        let submit = Submit(Mutex::new(vec![]));
        let r = reconcile_guild(&lookup, &submit, "111", Some("v1"))
            .await
            .unwrap();
        assert_eq!(
            r,
            Reconciled::Overwritten {
                version: "v2".into(),
                count: 2
            }
        );
        let sent = submit.0.lock().unwrap().clone();
        assert_eq!(sent.len(), 1);
        verify_submitted(&sent[0].1, &lookup.answer).unwrap();
        assert_eq!(lookup.asked.lock().unwrap()[0].as_deref(), Some("v1"));

        // Unchanged: hub says so, or the version is already registered -> no write.
        let same = Lookup {
            answer: Desired {
                version: "v2".into(),
                changed: false,
                commands: vec![],
            },
            asked: Mutex::new(vec![]),
        };
        let submit2 = Submit(Mutex::new(vec![]));
        assert_eq!(
            reconcile_guild(&same, &submit2, "111", Some("v2"))
                .await
                .unwrap(),
            Reconciled::Unchanged
        );
        let current = Lookup {
            answer: desired("v2", &set),
            asked: Mutex::new(vec![]),
        };
        assert_eq!(
            reconcile_guild(&current, &submit2, "111", Some("v2"))
                .await
                .unwrap(),
            Reconciled::Unchanged
        );
        assert!(submit2.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_refused_submit_does_not_advance_the_version() {
        struct Refuse;
        impl CommandOpSubmit for Refuse {
            fn submit<'a>(&'a self, _g: &'a str, _t: String) -> BoxFut<'a, Result<(), String>> {
                Box::pin(async { Err("refused".to_string()) })
            }
        }
        let lookup = Lookup {
            answer: desired("v2", &[cmd("context", "Switch")]),
            asked: Mutex::new(vec![]),
        };
        assert_eq!(
            reconcile_guild(&lookup, &Refuse, "111", None).await,
            Err(RegistryError::Unavailable)
        );
    }
}
