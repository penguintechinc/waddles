//! Shared per-RPC policy for the single-purpose hub-api gRPC clients
//! ([`crate::bundle_schedule`], [`crate::active_context`], [`crate::resolve_scope`]).
//!
//! Each of those clients owns one long-lived [`tonic::transport::Channel`] and delegates the same
//! four concerns here so they cannot drift apart:
//!
//! * **auth** -- [`RpcGuard::request`] mints/refreshes the machine JWT ([`MachineJwtClient`], with
//!   the client's own scope) and stamps `authorization: Bearer <jwt>` plus a bounded deadline
//!   (hub-api rejects anything over its 5s ceiling) onto the outgoing request;
//! * **fail fast** -- [`RpcGuard::admit`] consults a circuit breaker so a wedged hub-api is not
//!   piled onto;
//! * **accounting** -- [`RpcGuard::settle`] records the outcome on the breaker with the same
//!   classification as [`crate::HubClient`]: only server-health failures count, a request the
//!   server answered and declined ([`crate::is_caller_error`]) is breaker-neutral;
//! * **telemetry** -- `settle` returns the bounded outcome label the caller records on its tracing
//!   span (ids only; never a value from the request).

use std::sync::Arc;
use std::time::Duration;

use service_auth::MachineJwtClient;
use tonic::{Request, Status};

use crate::{is_caller_error, CircuitBreaker, HubClientError};

/// Default per-call deadline (hub-api's server-side ceiling is 5s).
pub(crate) const DEFAULT_DEADLINE: Duration = Duration::from_secs(3);

/// Breaker + token source + deadline shared by one single-purpose client's calls.
#[derive(Clone)]
pub(crate) struct RpcGuard {
    token_client: Arc<MachineJwtClient>,
    breaker: Arc<CircuitBreaker>,
    deadline: Duration,
}

impl RpcGuard {
    /// A guard with a fresh breaker (5 failures, 30s reset) and [`DEFAULT_DEADLINE`].
    pub(crate) fn new(token_client: Arc<MachineJwtClient>) -> Self {
        Self {
            token_client,
            breaker: Arc::new(CircuitBreaker::new(5, Duration::from_secs(30))),
            deadline: DEFAULT_DEADLINE,
        }
    }

    /// Replaces the per-call deadline.
    pub(crate) fn with_deadline(mut self, deadline: Duration) -> Self {
        self.deadline = deadline;
        self
    }

    /// `Err(CircuitOpen)` while the breaker is open (half-open lets one probe through).
    pub(crate) fn admit(&self) -> Result<(), HubClientError> {
        if self.breaker.allow() {
            Ok(())
        } else {
            Err(HubClientError::CircuitOpen)
        }
    }

    /// Wraps `message` in a request carrying the bearer token and the deadline.
    ///
    /// # Errors
    /// [`HubClientError::Auth`] when no machine JWT can be minted.
    pub(crate) async fn request<T>(&self, message: T) -> Result<Request<T>, HubClientError> {
        let token = self.token_client.get_token().await?;
        let mut request = Request::new(message);
        request.set_timeout(self.deadline);
        request.metadata_mut().insert(
            "authorization",
            format!("Bearer {token}")
                .parse()
                .map_err(|_| Status::internal("invalid token metadata"))?,
        );
        Ok(request)
    }

    /// Records `result` on the breaker and returns its bounded outcome label.
    pub(crate) fn settle<T>(&self, result: &Result<T, HubClientError>) -> String {
        match result {
            Ok(_) => {
                self.breaker.record_success();
                "ok".to_owned()
            }
            Err(HubClientError::Grpc(status)) if is_caller_error(status) => {
                format!("{:?}", status.code())
            }
            Err(err) => {
                self.breaker.record_failure();
                match err {
                    HubClientError::Grpc(status) => format!("{:?}", status.code()),
                    HubClientError::Auth(_) => "auth".to_owned(),
                    HubClientError::CircuitOpen => "circuit_open".to_owned(),
                    _ => "error".to_owned(),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn guard() -> RpcGuard {
        RpcGuard::new(Arc::new(MachineJwtClient::new(
            "http://127.0.0.1:1/token",
            "/nonexistent/sa-token",
            "scope",
        )))
    }

    fn grpc(code: tonic::Code) -> Result<(), HubClientError> {
        Err(HubClientError::Grpc(Status::new(code, "x")))
    }

    #[test]
    fn caller_errors_are_breaker_neutral_and_labelled() {
        let g = guard();
        for _ in 0..20 {
            assert_eq!(g.settle(&grpc(tonic::Code::NotFound)), "NotFound");
        }
        assert!(g.admit().is_ok());
    }

    #[test]
    fn server_failures_open_the_breaker_and_success_closes_it() {
        let g = guard();
        for _ in 0..5 {
            assert_eq!(g.settle(&grpc(tonic::Code::Unavailable)), "Unavailable");
        }
        assert!(matches!(g.admit(), Err(HubClientError::CircuitOpen)));
        assert_eq!(g.settle(&Ok(())), "ok");
        assert!(g.admit().is_ok());
    }

    #[test]
    fn auth_rejections_count_as_failures_not_caller_errors() {
        let g = guard();
        for _ in 0..5 {
            assert_eq!(
                g.settle(&grpc(tonic::Code::PermissionDenied)),
                "PermissionDenied"
            );
        }
        assert!(matches!(g.admit(), Err(HubClientError::CircuitOpen)));
    }

    #[tokio::test]
    async fn request_without_a_token_is_an_auth_error() {
        let g = guard();
        assert!(matches!(
            g.request(1_u8).await,
            Err(HubClientError::Auth(_))
        ));
    }

    #[test]
    fn deadline_stays_under_the_server_ceiling() {
        assert!(DEFAULT_DEADLINE < Duration::from_secs(5));
        let g = guard().with_deadline(Duration::from_millis(10));
        assert_eq!(g.deadline, Duration::from_millis(10));
    }
}
