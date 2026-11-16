//! First-door capture: how a receiver tells the single publish funnel "this event's clock started
//! HERE".
//!
//! A receiver (the Discord gateway loop, an HTTP webhook handler) wraps its handling of one frame in
//! [`with_first_door`]. `svc_ingest::publish::publish_resolved` -- the one funnel every ingest path
//! goes through -- reads [`current`] and stamps `t0` into the envelope's tracestate. A task-local
//! (not a parameter) keeps the twenty-odd `publish_event*` call sites and their tests untouched. When
//! no receiver set one (a path that has not been wired yet), the funnel stamps its own entry time and
//! marks the door `publish`: a lower bound, never a silent zero.

use std::future::Future;

use crate::stamp::{now_ms, Door, MAX_AGE_MS};

tokio::task_local! {
    static FIRST_DOOR: (u64, Door);
}

/// Runs `fut` with `(t0_ms, door)` as the current first door.
pub async fn with_first_door<F: Future>(t0_ms: u64, door: Door, fut: F) -> F::Output {
    FIRST_DOOR.scope((t0_ms, door), fut).await
}

/// The current first door, if a receiver set one for this task.
pub fn current() -> Option<(u64, Door)> {
    FIRST_DOOR.try_with(|v| *v).ok()
}

/// Parses a trusted proxy's request-start header into epoch ms. Accepts the common spellings:
/// `t=1731575000.123` (nginx `$msec`, Heroku `X-Request-Start`), `1731575000.123` (Envoy
/// `%START_TIME(%s.%3f)%`), `1731575000123` (epoch ms) and `1731575000` (epoch s). A value that is
/// not a plausible instant (future beyond skew, older than [`MAX_AGE_MS`], unparseable) is `None`
/// -- the caller then uses its own handler-entry time. The header is a hint from the proxy tier; it
/// is metadata only and never used for authorization.
pub fn parse_request_start(raw: &str, now: u64) -> Option<u64> {
    let v = raw.trim().strip_prefix("t=").unwrap_or(raw.trim());
    if v.is_empty() || v.len() > 24 {
        return None;
    }
    let ms: u64 = if let Some((secs, frac)) = v.split_once('.') {
        let secs: u64 = secs.parse().ok()?;
        let frac3: String = frac.chars().take(3).collect();
        if frac3.is_empty() || !frac3.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let frac_ms: u64 = format!("{frac3:0<3}").parse().ok()?;
        secs.checked_mul(1000)?.checked_add(frac_ms)?
    } else {
        let n: u64 = v.parse().ok()?;
        if n >= 100_000_000_000 {
            n
        } else {
            n.checked_mul(1000)?
        }
    };
    crate::stamp::elapsed_ms(ms, now).ok().map(|_| ms)
}

/// Convenience: [`parse_request_start`] against the current clock.
pub fn parse_request_start_now(raw: &str) -> Option<u64> {
    parse_request_start(raw, now_ms())
}

/// Largest believable gap between the edge stamp (Cilium LB / Gateway) and the ingest pod's handler
/// entry. Inside one cluster the leg is milliseconds; anything past this is a bad or replayed stamp
/// (it would otherwise inflate the SLA clock), so the handler's own entry time is used instead.
pub const MAX_EDGE_LEG_MS: u64 = 5_000;

/// Where the SLA clock starts for an HTTP-door event, and why.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EdgeStart {
    /// The first-door instant (epoch ms): the edge stamp when it is trusted and plausible, else the
    /// handler's entry time.
    pub t0_ms: u64,
    /// `true` when `t0_ms` is the edge stamp (the LB leg is inside the measurement).
    pub from_edge: bool,
    /// Why a trusted-but-unusable stamp was refused (`missing`, `skewed`, `stale`); `None` when the
    /// stamp was used or edge stamping is not trusted. Feed to `record_marker_invalid`.
    pub refused: Option<&'static str>,
}

/// Resolves the first door of an HTTP request from the edge-stamped request-start header.
///
/// `values` are ALL values of the header (the edge SETS it, so exactly one is expected; several mean
/// a client smuggled its own and are refused as `skewed`). With `trust_edge` false the header is
/// ignored entirely (a client could have sent it) and the handler entry time is the first door.
/// Never fails: every refusal falls back to `entry_ms`, so a bad stamp can only lose the LB leg.
pub fn resolve_edge_start(values: &[&str], entry_ms: u64, trust_edge: bool) -> EdgeStart {
    let fallback = |refused| EdgeStart {
        t0_ms: entry_ms,
        from_edge: false,
        refused,
    };
    if !trust_edge {
        return fallback(None);
    }
    let [raw] = values else {
        return fallback(Some(if values.is_empty() {
            "missing"
        } else {
            "skewed"
        }));
    };
    let Some(ms) = parse_request_start(raw, entry_ms) else {
        return fallback(Some("skewed"));
    };
    // A stamp a few ms ahead of this clock (cross-node drift) reads as "no edge leg".
    let t0 = ms.min(entry_ms);
    if entry_ms - t0 > MAX_EDGE_LEG_MS {
        return fallback(Some("stale"));
    }
    EdgeStart {
        t0_ms: t0,
        from_edge: true,
        refused: None,
    }
}

/// The sane upper bound a receiver should expect between its own entry time and a proxy stamp.
pub const MAX_PROXY_LEG_MS: u64 = MAX_AGE_MS;

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn current_is_set_inside_the_scope_only() {
        assert_eq!(current(), None);
        let seen = with_first_door(42, Door::Gateway, async { current() }).await;
        assert_eq!(seen, Some((42, Door::Gateway)));
        assert_eq!(current(), None);
    }

    #[tokio::test]
    async fn scope_is_inherited_across_awaits_in_the_same_task_but_not_spawned_tasks() {
        with_first_door(7, Door::Http, async {
            tokio::task::yield_now().await;
            assert_eq!(current(), Some((7, Door::Http)));
            let spawned = tokio::spawn(async { current() }).await.unwrap();
            assert_eq!(
                spawned, None,
                "spawned tasks do not inherit; the funnel falls back"
            );
        })
        .await;
    }

    #[test]
    fn request_start_header_spellings_parse_and_garbage_is_refused() {
        let now = 1_731_575_000_500;
        assert_eq!(
            parse_request_start("t=1731575000.123", now),
            Some(1_731_575_000_123)
        );
        assert_eq!(
            parse_request_start("1731575000.123", now),
            Some(1_731_575_000_123)
        );
        assert_eq!(
            parse_request_start("1731575000.5", now),
            Some(1_731_575_000_500)
        );
        assert_eq!(
            parse_request_start("1731575000123", now),
            Some(1_731_575_000_123)
        );
        assert_eq!(
            parse_request_start("1731575000", now),
            Some(1_731_575_000_000)
        );
        for bad in [
            "",
            "t=",
            "abc",
            "1731575000.",
            "1731575000.x1",
            "-5",
            &"9".repeat(30),
        ] {
            assert_eq!(parse_request_start(bad, now), None, "{bad:?}");
        }
    }

    #[test]
    fn forged_request_start_far_future_or_ancient_is_refused() {
        let now = 1_731_575_000_500;
        assert_eq!(parse_request_start("1731575999.000", now), None, "future");
        assert_eq!(parse_request_start("1000000000.000", now), None, "ancient");
    }

    #[test]
    fn edge_start_uses_a_plausible_trusted_stamp_and_never_fails() {
        let entry = 1_731_575_000_500;
        let ok = resolve_edge_start(&["t=1731575000.450"], entry, true);
        assert_eq!(
            (ok.t0_ms, ok.from_edge, ok.refused),
            (1_731_575_000_450, true, None)
        );
        // slightly in the future (cross-node skew) -> no edge leg, still "from edge"
        let skew = resolve_edge_start(&["1731575000.900"], entry, true);
        assert_eq!((skew.t0_ms, skew.from_edge), (entry, true));
        // untrusted: header ignored even when valid
        let off = resolve_edge_start(&["1731575000.450"], entry, false);
        assert_eq!(
            (off.t0_ms, off.from_edge, off.refused),
            (entry, false, None)
        );
        // refusals fall back to the entry instant with a reason
        for (vals, why) in [
            (vec![], "missing"),
            (vec!["1731575000.450", "1731575000.451"], "skewed"),
            (vec!["%START_TIME(%s.%3f)%"], "skewed"),
            (vec!["abc"], "skewed"),
            (vec!["1731574990.000"], "stale"),
        ] {
            let r = resolve_edge_start(&vals, entry, true);
            assert_eq!(
                (r.t0_ms, r.from_edge, r.refused),
                (entry, false, Some(why)),
                "{vals:?}"
            );
        }
    }
}
