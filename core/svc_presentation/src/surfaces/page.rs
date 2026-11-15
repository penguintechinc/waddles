//! The static overlay/webui page that renders surface frames
//! (`GET /surfaces/{overlay_code}/view`). The document is embedded and never
//! interpolated; its single inline script is allowed by a `sha256-` CSP hash
//! (no `'unsafe-inline'` scripts) and puts server strings into the DOM only
//! through `textContent`, so a payload cannot inject markup or script.

use std::sync::LazyLock;

use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use base64::Engine as _;
use sha2::{Digest, Sha256};

use crate::error::ApiError;

/// The embedded page.
pub const PAGE: &str = include_str!("surfaces-overlay.html");

/// Text between the single `<script>` and `</script>` (the CSP-hashed bytes);
/// `None` unless there is exactly one script block.
pub fn inline_script(page: &str) -> Option<&str> {
    if page.matches("<script").count() != 1 || page.matches("</script>").count() != 1 {
        return None;
    }
    let open = page.find("<script>")? + "<script>".len();
    let close = page.find("</script>")?;
    page.get(open..close)
}

fn build_csp(page: &str) -> Option<String> {
    let script = inline_script(page)?;
    let hash = base64::engine::general_purpose::STANDARD.encode(Sha256::digest(script.as_bytes()));
    Some(format!(
        "default-src 'none'; script-src 'sha256-{hash}'; style-src 'unsafe-inline'; \
         img-src https:; connect-src 'self'; base-uri 'none'; form-action 'none'"
    ))
}

static CSP: LazyLock<Option<String>> = LazyLock::new(|| build_csp(PAGE));

/// `GET /surfaces/{overlay_code}/view` -- served after the VIEW guard has
/// validated the key for the resolved community. Nothing from the request is
/// echoed into the response.
#[tracing::instrument(name = "surfaces.page", skip_all)]
pub async fn page() -> Result<Response, ApiError> {
    let csp = CSP.as_deref().ok_or_else(|| {
        ApiError::Internal(anyhow::anyhow!(
            "surfaces page template has no single inline script; refusing to serve without a CSP hash"
        ))
    })?;
    let csp = HeaderValue::from_str(csp)
        .map_err(|e| ApiError::Internal(anyhow::anyhow!("invalid surfaces CSP: {e}")))?;
    let mut response = (StatusCode::OK, PAGE).into_response();
    let h = response.headers_mut();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    h.insert(header::CONTENT_SECURITY_POLICY, csp);
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strip_comments(script: &str) -> String {
        let mut out = String::new();
        let mut rest = script;
        while let Some(s) = rest.find("/*") {
            out.push_str(&rest[..s]);
            match rest[s..].find("*/") {
                Some(e) => rest = &rest[s + e + 2..],
                None => {
                    rest = "";
                    break;
                }
            }
        }
        out.push_str(rest);
        out
    }

    #[test]
    fn csp_hash_matches_the_script_and_forbids_unsafe_script() {
        let csp = CSP.as_deref().expect("one script block");
        let expected = base64::engine::general_purpose::STANDARD
            .encode(Sha256::digest(inline_script(PAGE).unwrap().as_bytes()));
        assert!(csp.contains(&format!("script-src 'sha256-{expected}'")));
        assert!(!csp.contains("unsafe-eval"));
        assert!(!csp.contains("script-src 'unsafe-inline'"));
        assert!(csp.contains("default-src 'none'"));
        assert!(csp.contains("connect-src 'self'"));
    }

    #[test]
    fn script_has_no_markup_or_code_execution_sinks() {
        let script = strip_comments(inline_script(PAGE).unwrap());
        for banned in [
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
            "setTimeout('",
            "setTimeout(\"",
            "srcdoc",
            "createContextualFragment",
            "location.href",
            "location =",
        ] {
            assert!(!script.contains(banned), "page script uses `{banned}`");
        }
        assert!(script.contains("textContent"));
    }

    #[test]
    fn page_knows_exactly_the_contract_surfaces() {
        let contract = crate::surfaces::contract::SurfaceContract::load().unwrap();
        for id in contract.ids() {
            assert!(
                PAGE.contains(&format!("'{id}'")),
                "page lacks renderer for {id}"
            );
        }
    }

    #[tokio::test]
    async fn serves_with_security_headers() {
        let r = page().await.unwrap();
        assert_eq!(r.status(), StatusCode::OK);
        assert_eq!(r.headers().get(header::CACHE_CONTROL).unwrap(), "no-store");
        assert_eq!(
            r.headers().get(header::X_CONTENT_TYPE_OPTIONS).unwrap(),
            "nosniff"
        );
        assert!(r.headers().contains_key(header::CONTENT_SECURITY_POLICY));
    }
}
