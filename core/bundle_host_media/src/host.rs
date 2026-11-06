//! [`MediaHost`]: the orchestration the stage calls. Order for every generation:
//! validate -> tier gate -> BYOK key -> rate limit -> (job) -> provider -> output
//! validation -> tagged storage -> handle. Scope is always the host-derived
//! [`Scope`]; nothing the guest sends can widen it.

use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;

use crate::credentials::{ApiKey, CredentialError, CredentialResolver};
use crate::jobs::{JobRecord, JobStore, RecState};
use crate::metrics;
use crate::provider::{GeneratedMedia, MediaProvider, PollOutcome, ProviderError, SubmitOutcome};
use crate::store::{object_key, MediaStore};
use crate::tier::{require_professional, TierResolver};
use crate::types::{Handle, JobState, MediaError, MediaKind, Scope};
use crate::validate::{validate_output, validate_prompt};

/// Wall clock seam.
pub trait Clock: Send + Sync {
    /// Epoch milliseconds.
    fn now_ms(&self) -> u64;
}

/// System clock.
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> u64 {
        match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
            Ok(d) => millis(d),
            // A clock before 1970 is a broken host. 0 only delays the
            // read-side pending->timeout conversion; the worker's own timer
            // still bounds every job.
            Err(_) => 0,
        }
    }
}

/// Operator limits (never guest-influenced).
#[derive(Debug, Clone)]
pub struct MediaConfig {
    /// Synchronous image bound.
    pub image_timeout: Duration,
    /// Music job bound.
    pub music_timeout: Duration,
    /// Video job bound.
    pub video_timeout: Duration,
    /// Operation poll cadence.
    pub poll_interval: Duration,
    /// Submits per community per minute.
    pub rate_per_minute: u32,
    /// Concurrent jobs per community.
    pub max_inflight: u32,
    /// Job record TTL.
    pub job_ttl_secs: u64,
    /// Presigned URL TTL (clamped to 15 min).
    pub url_ttl: Duration,
}

impl Default for MediaConfig {
    fn default() -> Self {
        Self {
            image_timeout: Duration::from_secs(60),
            music_timeout: Duration::from_secs(300),
            video_timeout: Duration::from_secs(600),
            poll_interval: Duration::from_secs(5),
            rate_per_minute: 6,
            max_inflight: 4,
            job_ttl_secs: 3600,
            url_ttl: Duration::from_secs(300),
        }
    }
}

impl MediaConfig {
    fn timeout(&self, kind: MediaKind) -> Duration {
        match kind {
            MediaKind::Image => self.image_timeout,
            MediaKind::Music => self.music_timeout,
            MediaKind::Video => self.video_timeout,
        }
    }
}

/// The media capability host.
pub struct MediaHost {
    tier: Arc<dyn TierResolver>,
    creds: Arc<dyn CredentialResolver>,
    provider: Arc<dyn MediaProvider>,
    store: Arc<dyn MediaStore>,
    jobs: Arc<dyn JobStore>,
    clock: Arc<dyn Clock>,
    cfg: MediaConfig,
}

fn map_provider(e: ProviderError) -> MediaError {
    match e {
        ProviderError::Refused(r) => MediaError::ProviderRefused(r),
        ProviderError::Quota => MediaError::Quota("provider".into()),
        ProviderError::Timeout => MediaError::Timeout,
        ProviderError::Backend(m) => MediaError::Backend(m),
    }
}

fn outcome_label(e: &MediaError) -> &'static str {
    e.code()
}

impl MediaHost {
    /// Wires the host from its seams.
    pub fn new(
        tier: Arc<dyn TierResolver>,
        creds: Arc<dyn CredentialResolver>,
        provider: Arc<dyn MediaProvider>,
        store: Arc<dyn MediaStore>,
        jobs: Arc<dyn JobStore>,
        clock: Arc<dyn Clock>,
        cfg: MediaConfig,
    ) -> Self {
        Self {
            tier,
            creds,
            provider,
            store,
            jobs,
            clock,
            cfg,
        }
    }

    /// validate -> tier -> key -> rate. The key is returned zeroizing.
    async fn preflight(&self, scope: &Scope, prompt: &str) -> Result<ApiKey, MediaError> {
        validate_prompt(prompt)?;
        require_professional(self.tier.as_ref(), scope).await?;
        let provider = self.provider.name();
        let key = match self.creds.resolve(scope, provider).await {
            Ok(k) => k,
            Err(CredentialError::NotConfigured) => {
                return Err(MediaError::ByokMissing(provider.into()))
            }
            Err(CredentialError::Unavailable(m)) => {
                tracing::warn!(reason = %m, "media byok resolver unavailable");
                return Err(MediaError::Unavailable("credential resolver".into()));
            }
        };
        if !self
            .jobs
            .acquire_rate(scope, self.cfg.rate_per_minute, self.clock.now_ms())
            .await?
        {
            return Err(MediaError::Quota("minute".into()));
        }
        Ok(key)
    }

    /// Validates, stores (tagged for the requester) and returns the object handle.
    async fn finalize(
        &self,
        scope: &Scope,
        kind: MediaKind,
        job_id: &str,
        media: GeneratedMedia,
    ) -> Result<String, MediaError> {
        let sniffed = validate_output(kind, &media.bytes)?;
        if media.claimed_mime.split(';').next().map(str::trim) != Some(sniffed.mime) {
            tracing::debug!(
                kind = kind.as_str(),
                "provider mime differs from sniffed type; using sniffed"
            );
        }
        let handle = Handle {
            tenant_id: scope.tenant_id,
            community_id: scope.community_id,
            job_id: job_id.to_string(),
            ext: sniffed.ext.into(),
        };
        self.store
            .put(
                &object_key(&handle),
                Bytes::from(media.bytes),
                sniffed.mime,
                &scope.app_id,
            )
            .await
            .map_err(|e| {
                tracing::error!(error = %e, "media output store failed");
                MediaError::Unavailable("storage".into())
            })?;
        Ok(handle.render())
    }

    /// Synchronous image generation (bounded by `image_timeout`). Returns the handle.
    pub async fn generate_image(&self, scope: &Scope, prompt: &str) -> Result<String, MediaError> {
        let started = Instant::now();
        let res = async {
            let key = self.preflight(scope, prompt).await?;
            let media = match tokio::time::timeout(
                self.cfg.image_timeout,
                self.provider.generate_image(&key, prompt),
            )
            .await
            {
                Err(_) => return Err(MediaError::Timeout),
                Ok(r) => r.map_err(map_provider)?,
            };
            drop(key);
            self.finalize(scope, MediaKind::Image, &new_job_id(), media)
                .await
        }
        .await;
        metrics::record(
            "image",
            "generate",
            res.as_ref().map_or_else(outcome_label, |_| "ok"),
            started.elapsed().as_secs_f64(),
        );
        res
    }

    /// Starts a music/video job; returns its id. The worker runs detached; the
    /// guest polls [`Self::job_status`].
    pub async fn submit(
        self: &Arc<Self>,
        scope: &Scope,
        kind: MediaKind,
        prompt: &str,
    ) -> Result<String, MediaError> {
        if kind == MediaKind::Image {
            return Err(MediaError::Invalid(
                "image is synchronous; use generate-image".into(),
            ));
        }
        let res = self.submit_inner(scope, kind, prompt).await;
        if let Err(e) = &res {
            metrics::record(kind.as_str(), "submit", outcome_label(e), 0.0);
        }
        res
    }

    async fn submit_inner(
        self: &Arc<Self>,
        scope: &Scope,
        kind: MediaKind,
        prompt: &str,
    ) -> Result<String, MediaError> {
        let key = self.preflight(scope, prompt).await?;
        let job_id = new_job_id();
        let now = self.clock.now_ms();
        let deadline = now.saturating_add(millis(self.cfg.timeout(kind)));
        if !self
            .jobs
            .inflight_start(scope, &job_id, deadline, now, self.cfg.max_inflight)
            .await?
        {
            return Err(MediaError::Quota("inflight".into()));
        }
        let rec = JobRecord {
            app_id: scope.app_id.clone(),
            kind,
            deadline_ms: deadline,
            state: RecState::Pending,
        };
        if let Err(e) = self
            .jobs
            .put(scope, &job_id, &rec, self.cfg.job_ttl_secs)
            .await
        {
            if let Err(e2) = self.jobs.inflight_end(scope, &job_id).await {
                tracing::warn!(error = %e2, "media inflight release failed");
            }
            return Err(e);
        }
        let host = Arc::clone(self);
        let (scope, prompt, id) = (scope.clone(), prompt.to_string(), job_id.clone());
        tokio::spawn(async move { host.run_job(scope, id, kind, key, prompt).await });
        Ok(job_id)
    }

    async fn run_job(
        self: Arc<Self>,
        scope: Scope,
        job_id: String,
        kind: MediaKind,
        key: ApiKey,
        prompt: String,
    ) {
        let started = Instant::now();
        let outcome = match tokio::time::timeout(
            self.cfg.timeout(kind),
            self.drive(&scope, &job_id, kind, &key, &prompt),
        )
        .await
        {
            Ok(r) => r,
            Err(_) => Err(MediaError::Timeout),
        };
        drop(key);
        let (state, label) = match &outcome {
            Ok(handle) => (
                RecState::Done {
                    handle: handle.clone(),
                },
                "ok",
            ),
            Err(e) => (
                RecState::Failed {
                    code: e.code().into(),
                    detail: e.detail(),
                },
                outcome_label(e),
            ),
        };
        let deadline = self.clock.now_ms();
        let rec = JobRecord {
            app_id: scope.app_id.clone(),
            kind,
            deadline_ms: deadline,
            state,
        };
        if let Err(e) = self
            .jobs
            .put(&scope, &job_id, &rec, self.cfg.job_ttl_secs)
            .await
        {
            tracing::error!(error = %e, "media job result not persisted; job will read as timed out");
        }
        if let Err(e) = self.jobs.inflight_end(&scope, &job_id).await {
            tracing::warn!(error = %e, "media inflight release failed");
        }
        metrics::record(kind.as_str(), "job", label, started.elapsed().as_secs_f64());
    }

    async fn drive(
        &self,
        scope: &Scope,
        job_id: &str,
        kind: MediaKind,
        key: &ApiKey,
        prompt: &str,
    ) -> Result<String, MediaError> {
        let media = match self
            .provider
            .submit(kind, key, prompt)
            .await
            .map_err(map_provider)?
        {
            SubmitOutcome::Done(m) => m,
            SubmitOutcome::Running(op) => loop {
                tokio::time::sleep(self.cfg.poll_interval).await;
                match self
                    .provider
                    .poll(kind, key, &op)
                    .await
                    .map_err(map_provider)?
                {
                    PollOutcome::Pending => continue,
                    PollOutcome::Done(m) => break m,
                }
            },
        };
        self.finalize(scope, kind, job_id, media).await
    }

    /// Polls a job. Unknown ids and jobs of another bundle/community are `NotFound`.
    pub async fn job_status(&self, scope: &Scope, job_id: &str) -> Result<JobState, MediaError> {
        if !crate::types::is_job_id(job_id) {
            return Err(MediaError::NotFound);
        }
        let rec = self
            .jobs
            .get(scope, job_id)
            .await?
            .ok_or(MediaError::NotFound)?;
        if rec.app_id != scope.app_id {
            return Err(MediaError::NotFound);
        }
        match rec.state {
            RecState::Pending if self.clock.now_ms() > rec.deadline_ms => {
                Ok(JobState::Failed(MediaError::Timeout))
            }
            RecState::Pending => Ok(JobState::Pending),
            RecState::Done { handle } => Ok(JobState::Done(handle)),
            RecState::Failed { code, detail } => match MediaError::from_code(&code, detail) {
                Some(e) => Ok(JobState::Failed(e)),
                None => Ok(JobState::Failed(MediaError::Backend(
                    "unrecognized stored failure".into(),
                ))),
            },
        }
    }

    /// Resolves a handle to a short-TTL presigned GET URL -- only for the bundle
    /// the object is tagged for, inside its own tenant/community. Everything else
    /// is `NotFound`, indistinguishable from a missing object.
    pub async fn object_url(&self, scope: &Scope, handle: &str) -> Result<String, MediaError> {
        let h = Handle::parse(handle).ok_or(MediaError::NotFound)?;
        if h.tenant_id != scope.tenant_id || h.community_id != scope.community_id {
            return Err(MediaError::NotFound);
        }
        let key = object_key(&h);
        let owner = self.store.owner(&key).await.map_err(|e| {
            tracing::error!(error = %e, "media owner lookup failed");
            MediaError::Unavailable("storage".into())
        })?;
        match owner {
            Some(o) if o == scope.app_id => {}
            _ => return Err(MediaError::NotFound),
        }
        let ttl = self.cfg.url_ttl.min(Duration::from_secs(900));
        self.store.presign_get(&key, ttl).await.map_err(|e| {
            tracing::error!(error = %e, "media presign failed");
            MediaError::Unavailable("storage".into())
        })
    }
}

/// Milliseconds of `d`, saturating at `u64::MAX`.
fn millis(d: Duration) -> u64 {
    // `min` makes the narrowing exact: saturates instead of wrapping.
    d.as_millis().min(u128::from(u64::MAX)) as u64
}

fn new_job_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}
