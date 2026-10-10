//! The browser overlay page: `GET /overlay/{community}/{surface}?key=...`,
//! the URL an OBS browser source loads. One static, self-contained HTML
//! document serves every page-capable surface; it opens an `EventSource` on
//! the sibling `.../live` route (same `?key=`) and renders the sanitized
//! [`crate::overlay::render::RenderedFrame`]s that route streams.
//!
//! # Why the page can render server text as HTML
//!
//! A frame reaches `/live` only after the push route ran it through
//! [`crate::overlay::detok::OverlayDetokenizer`]: every free-text field is
//! HTML-escaped and every `{user:<uuid>}` placeholder is replaced by a
//! hub-api display name (or the neutral label), and `image_url` is validated
//! rather than escaped. The page therefore treats those fields as *already
//! escaped HTML* (the legacy Python `render.py` used `textContent` and would
//! show `&lt;` literally). Defense in depth, all pinned by tests below:
//!
//! * the document is **static** -- no community id, key or pushed text is ever
//!   interpolated into it, so there is no reflected-XSS surface;
//! * its single `<script>` is allowed by a **`sha256-` CSP hash** computed
//!   from the embedded bytes (no `'unsafe-inline'` scripts), so an injected
//!   inline handler or script would not run;
//! * the script has exactly one `innerHTML` sink, fed only through a
//!   `safeHtml()` helper that re-escapes anything carrying a raw `< > " '`;
//! * `Cache-Control: no-store` + `Referrer-Policy: no-referrer`, because the
//!   page URL carries the VIEW key.
//!
//! Surfaces without a browser page (`music`: poll-driven; `image`: fail-loud
//! stub; `caption`: its own `/overlay/captions/{key}` page) answer 404 -- a
//! loud "no such page", never a blank overlay.

use std::sync::LazyLock;

use axum::extract::Path;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use base64::Engine as _;
use overlay_schema::Surface;
use sha2::{Digest, Sha256};

use crate::error::ApiError;
use crate::http::overlay::OverlayRouteParams;

/// The OBS browser-source page, embedded at compile time. Static: it holds no
/// community data, key or pushed text.
const OVERLAY_PAGE: &str = include_str!("templates/overlay.html");

/// Surfaces that have a browser page. The template's `SURFACES` array must
/// list exactly these (a unit test enforces it).
pub const PAGE_SURFACES: [Surface; 7] = [
    Surface::AlertBox,
    Surface::Chat,
    Surface::Goals,
    Surface::Ticker,
    Surface::Crawler,
    Surface::FullScreen,
    Surface::Media,
];

/// `true` when `surface` has a browser page.
pub fn has_page(surface: Surface) -> bool {
    PAGE_SURFACES.contains(&surface)
}

/// The text between the page's one `<script>` and `</script>` -- exactly the
/// bytes a browser hashes for a CSP `sha256-` source. `None` if the template
/// does not contain exactly one script block (caught by a unit test; at
/// runtime it fails the request loudly rather than serving a page whose CSP
/// would block its own script).
fn inline_script(page: &str) -> Option<&str> {
    if page.matches("<script").count() != 1 || page.matches("</script>").count() != 1 {
        return None;
    }
    let open = page.find("<script>")? + "<script>".len();
    let close = page.find("</script>")?;
    page.get(open..close)
}

/// The page's `Content-Security-Policy`: nothing loadable by default; the one
/// inline script is allowed by hash; styles may be inline (CSS cannot run
/// script); images may come from any http(s) origin (a pushed `image_url`);
/// the only connection allowed is same-origin (the SSE stream).
/// `frame-ancestors` is deliberately omitted -- OBS embeds the page.
fn build_csp(page: &str) -> Option<String> {
    let script = inline_script(page)?;
    let digest = Sha256::digest(script.as_bytes());
    let hash = base64::engine::general_purpose::STANDARD.encode(digest);
    Some(format!(
        "default-src 'none'; script-src 'sha256-{hash}'; style-src 'unsafe-inline'; \
         img-src http: https:; connect-src 'self'; base-uri 'none'; form-action 'none'"
    ))
}

static PAGE_CSP: LazyLock<Option<String>> = LazyLock::new(|| build_csp(OVERLAY_PAGE));

/// `GET /overlay/{community}/{surface}` -- serves the overlay page once the
/// VIEW guard (mounted around this route in [`crate::http::router`]) has
/// validated `?key=` for this community. The path segments are only used to
/// pick 404 vs. page; nothing from them is echoed into the response.
#[tracing::instrument(name = "overlay.page", skip_all)]
pub async fn page(Path(params): Path<OverlayRouteParams>) -> Result<Response, ApiError> {
    let surface = Surface::ALL
        .iter()
        .copied()
        .find(|s| s.as_str() == params.surface)
        .ok_or_else(|| ApiError::NotFound("unknown surface".to_string()))?;
    if !has_page(surface) {
        tracing::debug!(
            surface = surface.as_str(),
            "no browser page for this surface"
        );
        return Err(ApiError::NotFound(format!(
            "surface {} has no browser overlay page",
            surface.as_str()
        )));
    }
    let Some(csp) = PAGE_CSP.as_deref() else {
        return Err(ApiError::Internal(anyhow::anyhow!(
            "overlay page template has no single inline script; refusing to serve it without a CSP hash"
        )));
    };
    let csp = HeaderValue::from_str(csp)
        .map_err(|err| ApiError::Internal(anyhow::anyhow!("invalid page CSP header: {err}")))?;
    tracing::debug!(surface = surface.as_str(), "serving overlay page");
    Ok(page_response(csp))
}

/// The embedded page with its security headers.
fn page_response(csp: HeaderValue) -> Response {
    let mut response = (StatusCode::OK, OVERLAY_PAGE).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(header::CONTENT_SECURITY_POLICY, csp);
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;

    fn path(surface: &str) -> Path<OverlayRouteParams> {
        Path(OverlayRouteParams {
            community: "42".to_string(),
            surface: surface.to_string(),
        })
    }

    #[tokio::test]
    async fn every_page_surface_is_served_with_security_headers() {
        for surface in PAGE_SURFACES {
            let response = match page(path(surface.as_str())).await {
                Ok(response) => response,
                Err(e) => panic!("{surface} should serve: {e}"),
            };
            assert_eq!(response.status(), StatusCode::OK, "{surface}");
            let headers = response.headers();
            assert_eq!(
                headers.get(header::CONTENT_TYPE).unwrap(),
                "text/html; charset=utf-8"
            );
            assert_eq!(headers.get(header::CACHE_CONTROL).unwrap(), "no-store");
            assert_eq!(
                headers.get(header::X_CONTENT_TYPE_OPTIONS).unwrap(),
                "nosniff"
            );
            assert_eq!(headers.get(header::REFERRER_POLICY).unwrap(), "no-referrer");
            assert!(headers.contains_key(header::CONTENT_SECURITY_POLICY));
        }
    }

    #[tokio::test]
    async fn the_page_body_is_the_static_template_and_echoes_nothing() {
        // A hostile surface segment is rejected, never reflected.
        let err = page(path("<script>alert(1)</script>")).await.unwrap_err();
        let response = err.into_response();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8_lossy(&body);
        assert!(!text.contains("<script>"), "reflected input: {text}");

        let ok = page(path("chat")).await.unwrap();
        let body = to_bytes(ok.into_body(), usize::MAX).await.unwrap();
        assert_eq!(&body[..], OVERLAY_PAGE.as_bytes());
    }

    #[tokio::test]
    async fn surfaces_without_a_page_are_a_loud_404() {
        for surface in [Surface::Music, Surface::Image, Surface::Caption] {
            let err = page(path(surface.as_str())).await.unwrap_err();
            assert_eq!(
                err.into_response().status(),
                StatusCode::NOT_FOUND,
                "{surface}"
            );
        }
    }

    #[test]
    fn page_surfaces_and_the_templates_surface_list_agree() {
        let list_line = OVERLAY_PAGE
            .lines()
            .find(|l| l.starts_with("var SURFACES = ["))
            .expect("template declares SURFACES");
        let in_template: Vec<&str> = list_line
            .trim_start_matches("var SURFACES = [")
            .trim_end_matches("];")
            .split(',')
            .map(|s| s.trim().trim_matches('\''))
            .collect();
        let in_rust: Vec<&str> = PAGE_SURFACES.iter().map(|s| s.as_str()).collect();
        assert_eq!(in_template, in_rust);
        // Every non-page surface is accounted for.
        for surface in Surface::ALL {
            assert_eq!(
                has_page(*surface),
                in_template.contains(&surface.as_str()),
                "{surface}"
            );
        }
    }

    #[test]
    fn csp_hash_matches_the_inline_script_exactly() {
        let csp = PAGE_CSP.as_deref().expect("template has one script block");
        let script = inline_script(OVERLAY_PAGE).unwrap();
        let expected =
            base64::engine::general_purpose::STANDARD.encode(Sha256::digest(script.as_bytes()));
        assert!(
            csp.contains(&format!("script-src 'sha256-{expected}'")),
            "{csp}"
        );
        assert!(!csp.contains("script-src 'unsafe-inline'"), "{csp}");
        assert!(!csp.contains("unsafe-eval"), "{csp}");
        assert!(csp.contains("default-src 'none'"), "{csp}");
        assert!(csp.contains("connect-src 'self'"), "{csp}");
        assert!(
            !csp.contains("frame-ancestors"),
            "OBS embeds the page: {csp}"
        );
    }

    #[test]
    fn strip_block_comments_removes_only_comments() {
        assert_eq!(strip_block_comments("a /* x */ b /* y */ c"), "a  b  c");
        assert_eq!(strip_block_comments("a /* unterminated"), "a ");
        assert_eq!(strip_block_comments("no comments"), "no comments");
    }

    #[test]
    fn inline_script_requires_exactly_one_script_block() {
        assert_eq!(
            inline_script("<html><script>let a=1;</script></html>"),
            Some("let a=1;")
        );
        assert_eq!(inline_script("<html></html>"), None);
        assert_eq!(inline_script("<script>a</script><script>b</script>"), None);
        assert_eq!(inline_script("<script src=\"x.js\"></script>"), None);
        assert!(build_csp("<html></html>").is_none());
    }

    /// `script` with every `/* ... */` block removed, so prose in comments
    /// can't trip (or hide from) the sink checks.
    fn strip_block_comments(script: &str) -> String {
        let mut out = String::with_capacity(script.len());
        let mut rest = script;
        while let Some(start) = rest.find("/*") {
            out.push_str(&rest[..start]);
            match rest[start..].find("*/") {
                Some(end) => rest = &rest[start + end + 2..],
                None => {
                    rest = "";
                    break;
                }
            }
        }
        out.push_str(rest);
        out
    }

    /// The template's script only ever puts server text into the DOM through
    /// the pinned sinks. A new `innerHTML`/`eval`/... use fails this test.
    #[test]
    fn the_script_uses_only_the_vetted_dom_sinks() {
        let script = strip_block_comments(inline_script(OVERLAY_PAGE).unwrap());
        let script = script.as_str();
        for banned in [
            "eval(",
            "new Function",
            "document.write",
            "outerHTML",
            "insertAdjacentHTML",
            "setAttribute('on",
            "setAttribute(\"on",
            "setAttribute('style",
            "javascript:",
            "localStorage",
            "sessionStorage",
            "postMessage",
        ] {
            assert!(!script.contains(banned), "banned construct: {banned}");
        }
        assert_eq!(
            script.matches("innerHTML").count(),
            1,
            "innerHTML may only appear in the setHtml sink"
        );
        assert!(script.contains("el.innerHTML = html;"));
        assert_eq!(script.matches("img.src =").count(), 1);
        assert!(
            script.contains("validImageUrl(frame.image_url)"),
            "img.src must be gated by validImageUrl"
        );
        // Every server-sent string field reaches the DOM only through
        // `safeHtml(frame.<field>)`; any other mention of the field must be a
        // plain truthiness check (`if (frame.f)`, `!frame.f`, `frame.f ?`).
        for field in [
            "alert_type",
            "display_name",
            "message",
            "platform",
            "text",
            "label",
            "unit",
            "title",
            "body",
        ] {
            let total = script.matches(&format!("frame.{field}")).count();
            let safe = script.matches(&format!("safeHtml(frame.{field}")).count();
            let truthy = script.matches(&format!("if (frame.{field})")).count()
                + script.matches(&format!("!frame.{field}")).count()
                + script.matches(&format!("frame.{field} ?")).count();
            assert!(total > 0, "template no longer renders `{field}`");
            assert_eq!(
                total,
                safe + truthy,
                "`frame.{field}` is used outside safeHtml()/a truthiness check"
            );
        }
        // Numbers are formatted by the script itself, never echoed from text.
        assert!(script.contains("finiteNumber(frame.current)"));
        assert!(script.contains("finiteNumber(frame.target)"));
        assert!(script.contains("formatAmount(frame.amount)"));
    }

    #[test]
    fn the_page_is_static_no_interpolation_slots() {
        for slot in ["{{", "}}", "__COMMUNITY__", "__KEY__", "${"] {
            assert!(!OVERLAY_PAGE.contains(slot), "template slot {slot}");
        }
    }
}
