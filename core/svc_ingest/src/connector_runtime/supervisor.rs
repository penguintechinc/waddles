//! The reconcile loop and per-connection supervision (spec section 3).

use std::sync::Arc;

use tokio::sync::oneshot;

use super::feed::BindingFeed;
use super::ready::{self, RuntimeReady};
use crate::connector::{ConnectorInvoker, EventSink};
use crate::twitch_sessions::SessionLease;

/// The connector runtime: reconciles desired connections and supervises one
/// driver per connection.
pub struct ConnectorRuntime<
    F: BindingFeed,
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
> {
    feed: F,
    invoker: Arc<I>,
    lease: Arc<L>,
    _sink: std::marker::PhantomData<S>,
}

impl<
        F: BindingFeed,
        I: ConnectorInvoker + 'static,
        S: EventSink + 'static,
        L: SessionLease + 'static,
    > ConnectorRuntime<F, I, S, L>
{
    /// Runs until `shutdown` resolves. Skeleton: reports loudly and returns.
    pub async fn run(self, _shutdown: oneshot::Receiver<()>) {
        let _ = (&self.feed, &self.invoker, &self.lease);
        tracing::error!("connector runtime is not implemented in this commit");
    }

    /// Shared readiness view for `/readyz`.
    pub fn ready_handle(&self) -> Arc<dyn RuntimeReady> {
        ready::pending()
    }
}
