//! Read-only Postgres connection factory for the DB-driven active-bundle
//! loader. Mirrors `core/svc_action/src/db/mod.rs`'s `connect`/URL-building
//! shape, with one deliberate difference: the config is its own small
//! struct (not the service's full `Config`, since this crate is shared by
//! two services with two otherwise-unrelated `Config` types).
//!
//! **Read-only defense-in-depth mechanism (security review fix):** every
//! physical connection in the pool -- not just the first one -- must be
//! session-read-only, as a code-level backstop alongside the RO account's
//! own grants (crate root doc). A one-shot `SET default_transaction_read_
//! only = on` issued right after `Database::connect` (the original
//! approach here) only ever touches whichever single connection answers
//! that one query; with `max_connections(4)`, connections 2-4 are never
//! touched by it and stay session-read-write (their commands would still
//! be rejected by the RO account's grants, but that's the DB-level
//! backstop, not this one). Fixed by moving the setting into the
//! connection URL itself via libpq/sqlx's `options[KEY]=VALUE` query
//! parameter (`sqlx_postgres::options::parse::parse_from_url`'s `k if
//! k.starts_with("options[")` branch, confirmed against that crate's own
//! source -- not `PgConnectOptions::options()`'s Rust builder method,
//! which `ConnectOptions::new(url)` never gets a chance to call): this
//! becomes part of the one `PgConnectOptions` sqlx re-applies to *every*
//! new physical connection it opens for the pool, so all four (or four
//! hundred) get `-c default_transaction_read_only=on` at startup-parameter
//! time, before this crate's code runs a single query against any of
//! them.
use sea_orm::{ConnectOptions, Database, DatabaseConnection, DbErr};

/// Non-secret connection settings for the reader endpoint -- `DB_READER_*`
/// in each service's `CliConfig` (or `DB_*`/primary as the alpha default;
/// see each service's own `config.rs` doc for the reader-vs-primary
/// fallback). The password is never carried on this struct -- callers pass
/// it directly to [`connect`], matching `core/svc_action/src/db`'s
/// `Secret`-never-on-CliConfig convention (Token & Secret Hygiene).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReaderConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    pub user: String,
}

/// `options[default_transaction_read_only]=on` -- see this module's doc
/// for why this is a query-string startup parameter and not a post-connect
/// `SET`/`execute_unprepared` call. `[`/`]` need no percent-encoding in a
/// URL query component (WHATWG URL query percent-encode set doesn't
/// include them; confirmed against `sqlx_postgres`'s own parser, which
/// reads query keys/values through the `url` crate's `query_pairs()`).
const READ_ONLY_OPTIONS_QUERY: &str = "options[default_transaction_read_only]=on";

fn connection_url(cfg: &ReaderConfig, password: &str) -> String {
    format!(
        "postgres://{user}:{password}@{host}:{port}/{name}?{ro_opts}",
        user = cfg.user,
        password = password,
        host = cfg.host,
        port = cfg.port,
        name = cfg.name,
        ro_opts = READ_ONLY_OPTIONS_QUERY,
    )
}

/// Opens a pooled, read-only connection: `sqlx_logging(false)` (never echo
/// the password-bearing URL at DEBUG, same rationale as
/// `core/svc_action/src/db::connect`), a small pool (this loader issues at
/// most one query per poll tick per service instance -- no need for the
/// primary connection's 10-connection pool), and every physical connection
/// session-read-only from the moment it's established (see [`connection_url`]
/// / [`READ_ONLY_OPTIONS_QUERY`] -- this module's doc has the full
/// mechanism rationale).
pub async fn connect(cfg: &ReaderConfig, password: &str) -> Result<DatabaseConnection, DbErr> {
    let mut opts = ConnectOptions::new(connection_url(cfg, password));
    opts.max_connections(4)
        .min_connections(1)
        .sqlx_logging(false);
    Database::connect(opts).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> ReaderConfig {
        ReaderConfig {
            host: "db-reader.internal".to_string(),
            port: 5433,
            name: "waddlebot".to_string(),
            user: "svc_process_ro".to_string(),
        }
    }

    #[test]
    fn connection_url_includes_configured_reader_host_and_user() {
        let url = connection_url(&sample(), "s3cret");
        assert_eq!(
            url,
            "postgres://svc_process_ro:s3cret@db-reader.internal:5433/waddlebot\
             ?options[default_transaction_read_only]=on"
        );
    }

    /// Security review fix: every connection the pool opens must be
    /// session-read-only from the URL sqlx parses, not from a one-shot
    /// post-connect `SET` that only ever reaches one physical connection.
    /// Parses the built URL with the real `url` crate (the same parser
    /// `sqlx_postgres::PgConnectOptions::parse_from_url` uses) rather than
    /// asserting a raw substring, so this test would catch a future change
    /// that reorders/escapes the query string in a way that broke
    /// `options[key]=value` parsing.
    #[test]
    fn connection_url_carries_the_read_only_startup_option_sqlx_will_parse() {
        let url = url::Url::parse(&connection_url(&sample(), "s3cret"))
            .expect("connection_url must always produce a valid URL");
        let value = url
            .query_pairs()
            .find(|(k, _)| k == "options[default_transaction_read_only]")
            .map(|(_, v)| v.into_owned());
        assert_eq!(value.as_deref(), Some("on"));
    }
}
