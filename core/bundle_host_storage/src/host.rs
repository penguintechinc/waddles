//! `StorageHost`: the single enforcement point. EVERY op goes through
//! [`StorageHost::resolve`] (flag, key validation, grant, rate limit, audit of
//! cross-prefix denials) and every read-shaped op through
//! [`StorageHost::require_visible`] -- the ONE tag filter. Universal invariant:
//! a cross-prefix caller only ever sees objects tagged `app-bundle:<caller>`;
//! an approved grant alone never exposes anything. Owners see all of their own.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;

use crate::audit::{AuditEvent, AuditOutcome, AuditSink};
use crate::backend::{ObjectBackend, ObjectMeta};
use crate::denial_queue::DenialQueue;
use crate::error::StorageError;
use crate::key::{object_key, scope_prefix, validate_key, validate_key_prefix, validate_owner};
use crate::limits::*;
use crate::metrics;
use crate::policy::{authorize, GrantSet, Op};
use crate::seal::{sealed_len, ObjectSealing};
use crate::tags::{
    bundle_tag, normalize_tags, system_tag, validate_request_id, validate_tag, MAX_TAGS,
    TAG_COMMUNITY, TAG_REQUEST_ID, TAG_SEALED, TAG_TENANT,
};

/// Host-derived invocation scope; NEVER built from guest args.
#[derive(Clone, Debug)]
pub struct StorageScope {
    pub app_id: String,
    pub tenant: u64,
    /// `0` = tenant-wide activation.
    pub community: u64,
    /// Approved cross-prefix grants (gate snapshot).
    pub grants: GrantSet,
    /// `waddles.bundle-object-storage` flag state.
    pub enabled: bool,
    /// Host-assigned request id (provenance tag `waddles-request-id`; never used for access).
    pub request_id: String,
}

/// One listed object (guest-visible: key relative to the prefix owner's scope).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ObjectInfo {
    pub key: String,
    pub size: u64,
    pub content_type: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListPage {
    pub objects: Vec<ObjectInfo>,
    pub next_cursor: Option<String>,
}

type RateKey = (String, u64, u64);

struct Resolved {
    owner: String,
    cross: bool,
    full_key: String,
}

pub struct StorageHost {
    backend: Arc<dyn ObjectBackend>,
    audit: Arc<dyn AuditSink>,
    denials: DenialQueue,
    rate: Mutex<HashMap<RateKey, (Instant, u32)>>,
    rate_limit: u32,
    sealing: Option<ObjectSealing>,
}

/// Guest-visible metadata; a sealed object's size is reported without the envelope overhead.
fn info(rel: &str, m: &ObjectMeta) -> ObjectInfo {
    let sealed = m.tags.contains(&system_tag(TAG_SEALED, 1));
    ObjectInfo {
        key: rel.to_string(),
        size: if sealed {
            m.size.saturating_sub(sealed_len(0))
        } else {
            m.size
        },
        content_type: m.content_type.clone(),
    }
}

impl StorageHost {
    pub fn new(backend: Arc<dyn ObjectBackend>, audit: Arc<dyn AuditSink>) -> Self {
        Self::with_rate_limit(backend, audit, RATE_OPS_PER_SEC)
    }

    pub fn with_rate_limit(
        backend: Arc<dyn ObjectBackend>,
        audit: Arc<dyn AuditSink>,
        rate_limit: u32,
    ) -> Self {
        Self {
            backend,
            denials: DenialQueue::new(audit.clone()),
            audit,
            rate: Mutex::new(HashMap::new()),
            rate_limit,
            sealing: None,
        }
    }

    /// Seals object bodies per tenant (see [`crate::seal`]); without it bodies are stored as
    /// given. Production wiring always calls this.
    #[must_use]
    pub fn with_sealing(mut self, sealing: ObjectSealing) -> Self {
        self.sealing = Some(sealing);
        self
    }

    /// Fail CLOSED for a principal whose denial records overflowed and are not yet
    /// durably recorded (see `denial_queue`): refuse before any other work.
    fn audit_backlog_check(&self, s: &StorageScope) -> Result<(), StorageError> {
        if self.denials.is_blocked(&s.app_id, s.tenant) {
            metrics::denied("audit_backlog");
            return Err(StorageError::RateLimited);
        }
        Ok(())
    }

    fn rate_check(&self, s: &StorageScope) -> Result<(), StorageError> {
        let mut m = self
            .rate
            .lock()
            .map_err(|_| StorageError::Backend("poisoned".into()))?;
        let e = m
            .entry((s.app_id.clone(), s.tenant, s.community))
            .or_insert((Instant::now(), 0));
        if e.0.elapsed() >= Duration::from_secs(1) {
            *e = (Instant::now(), 0);
        }
        if e.1 >= self.rate_limit {
            metrics::denied("rate_limited");
            return Err(StorageError::RateLimited);
        }
        e.1 += 1;
        Ok(())
    }

    async fn audit_raw(
        &self,
        s: &StorageScope,
        owner: &str,
        op: &'static str,
        key: &str,
        outcome: AuditOutcome,
        reason: &str,
    ) -> Result<(), StorageError> {
        self.audit
            .record(AuditEvent {
                caller: s.app_id.clone(),
                owner: owner.to_string(),
                tenant: s.tenant,
                community: s.community,
                op,
                key: key.to_string(),
                outcome,
                reason: reason.to_string(),
                request_id: s.request_id.clone(),
            })
            .await
    }

    /// Denials are audited too -- via the bounded drop-on-full queue so attacker-
    /// triggered denials can never starve the fail-closed allowed-path audits.
    async fn audit_denied(
        &self,
        s: &StorageScope,
        owner: &str,
        op: &'static str,
        key: &str,
        outcome: AuditOutcome,
        reason: &str,
    ) {
        self.denials.enqueue(AuditEvent {
            caller: s.app_id.clone(),
            owner: owner.to_string(),
            tenant: s.tenant,
            community: s.community,
            op,
            key: key.to_string(),
            outcome,
            reason: reason.to_string(),
            request_id: s.request_id.clone(),
        });
    }

    /// Waits until every denial record enqueued so far has been written.
    pub async fn flush_denials(&self) {
        self.denials.flush().await;
    }

    /// Flag, owner/key validation, grant, rate limit; builds the host-scoped key.
    /// Defence in depth: a zero tenant/community is the fail-closed "unresolved" scope
    /// and must never address a real prefix, on ANY op.
    fn require_scope(s: &StorageScope) -> Result<(), StorageError> {
        if s.tenant == 0 || s.community == 0 {
            metrics::denied("unscoped");
            return Err(StorageError::Invalid(
                "tenant and community scope required".into(),
            ));
        }
        Ok(())
    }

    async fn resolve(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        op: Op,
        key: &str,
    ) -> Result<Resolved, StorageError> {
        if !s.enabled {
            metrics::denied("flag_off");
            return Err(StorageError::NotImplemented);
        }
        Self::require_scope(s)?;
        let owner = owner.unwrap_or(&s.app_id).to_string();
        validate_owner(&owner)?;
        validate_key(key).inspect_err(|_| metrics::denied("invalid_key"))?;
        // Rate-limit BEFORE authorize/denial-audit: denied calls cost budget too.
        self.audit_backlog_check(s)?;
        self.rate_check(s)?;
        let cross = match authorize(&s.app_id, &owner, op, &s.grants) {
            Ok(c) => c,
            Err(e) => {
                metrics::denied("not_granted");
                self.audit_denied(
                    s,
                    &owner,
                    op.as_str(),
                    key,
                    AuditOutcome::Denied,
                    "no approved grant",
                )
                .await;
                return Err(e);
            }
        };
        let full_key = object_key(&owner, s.tenant, s.community, key);
        Ok(Resolved {
            owner,
            cross,
            full_key,
        })
    }

    /// THE tag filter. Cross-prefix: object must carry `app-bundle:<caller>`;
    /// otherwise indistinguishable from absent. Own prefix: always visible.
    ///
    /// Defense in depth, for owners too: the immutable host-written system tags
    /// `waddles-tenant`/`waddles-community` must match the caller's VERIFIED scope
    /// (an object missing them -- e.g. a path mistake or legacy -- is hidden).
    fn visible(s: &StorageScope, r_cross: bool, tags: &[String]) -> bool {
        tags.contains(&system_tag(TAG_TENANT, s.tenant))
            && tags.contains(&system_tag(TAG_COMMUNITY, s.community))
            && (!r_cross || tags.contains(&bundle_tag(&s.app_id)))
    }

    /// Head with the visibility filter applied (used by get/head/presign/delete/tag ops).
    async fn require_visible(
        &self,
        s: &StorageScope,
        r: &Resolved,
        op: &'static str,
        key: &str,
    ) -> Result<ObjectMeta, StorageError> {
        let meta = self
            .backend
            .head(&r.full_key)
            .await?
            .ok_or(StorageError::NotFound)?;
        if !Self::visible(s, r.cross, &meta.tags) {
            metrics::denied("tag_hidden");
            self.audit_denied(
                s,
                &r.owner,
                op,
                key,
                AuditOutcome::Denied,
                "object not tagged for caller",
            )
            .await;
            return Err(StorageError::NotFound);
        }
        Ok(meta)
    }

    /// Cross-prefix allowed access is audited DURABLY; a sink failure fails the op
    /// (mutations call this BEFORE the effect, reads before returning data).
    async fn audit_allowed(
        &self,
        s: &StorageScope,
        r: &Resolved,
        op: &'static str,
        key: &str,
    ) -> Result<(), StorageError> {
        if r.cross {
            self.audit_raw(
                s,
                &r.owner,
                op,
                key,
                AuditOutcome::Allowed,
                "approved grant + tag",
            )
            .await?;
        }
        Ok(())
    }

    /// Scope quota: total bytes/objects under the scope prefix, excluding `except`.
    async fn check_quota(
        &self,
        prefix: &str,
        except: &str,
        adding: u64,
    ) -> Result<(), StorageError> {
        let (mut bytes, mut count, mut after) = (adding, 1usize, None::<String>);
        loop {
            let page = self
                .backend
                .list(prefix, after.as_deref(), MAX_LIST_SCAN)
                .await?;
            let Some(last) = page.last().map(|(k, _)| k.clone()) else {
                break;
            };
            for (k, sz) in &page {
                if k != except {
                    bytes += sz;
                    count += 1;
                }
            }
            let full = page.len() == MAX_LIST_SCAN;
            after = Some(last);
            if !full || count > MAX_SCOPE_OBJECTS {
                break;
            }
        }
        if bytes > MAX_SCOPE_BYTES {
            metrics::quota_rejected("bytes");
            return Err(StorageError::QuotaExceeded);
        }
        if count > MAX_SCOPE_OBJECTS {
            metrics::quota_rejected("objects");
            return Err(StorageError::QuotaExceeded);
        }
        Ok(())
    }

    /// Stores an object. Own prefix: producer tags honoured. Cross-prefix: tags
    /// forced to `[app-bundle:<caller>]` and an existing non-visible object is never overwritten.
    pub async fn put(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
        body: Bytes,
        content_type: &str,
        tags: &[String],
    ) -> Result<ObjectInfo, StorageError> {
        let t0 = Instant::now();
        let r = self.resolve(s, owner, Op::Write, key).await?;
        if body.len() as u64 > MAX_OBJECT_BYTES {
            return Err(StorageError::TooLarge(body.len() as u64));
        }
        if !content_type_allowed(content_type) {
            metrics::denied("content_type");
            return Err(StorageError::Invalid("content-type not allowed".into()));
        }
        let tags = if r.cross {
            if !tags.is_empty() {
                return Err(StorageError::Invalid(
                    "tags may only be set on your own prefix".into(),
                ));
            }
            if let Some(existing) = self.backend.head(&r.full_key).await? {
                if !Self::visible(s, true, &existing.tags) {
                    metrics::denied("tag_hidden");
                    self.audit_denied(
                        s,
                        &r.owner,
                        "put",
                        key,
                        AuditOutcome::Denied,
                        "would overwrite untagged object",
                    )
                    .await;
                    return Err(StorageError::NotGranted("object not writable".into()));
                }
            }
            vec![bundle_tag(&s.app_id)]
        } else {
            normalize_tags(tags)?
        };
        validate_request_id(&s.request_id)?;
        let mut tags = tags;
        let size = body.len() as u64;
        let body = match &self.sealing {
            Some(sealing) if sealing.writes_sealed().await => {
                tags.push(system_tag(TAG_SEALED, 1));
                sealing
                    .seal(s.tenant, s.community, &r.owner, key, &body)
                    .await?
            }
            Some(_) => {
                // Owner policy: crypto fails CLOSED. Sealing switched off / unavailable means
                // the write is refused -- there is no plaintext write path.
                metrics::denied("sealing_unavailable");
                tracing::error!(
                    tenant = s.tenant,
                    "object write refused: tenant at-rest encryption is disabled/unavailable"
                );
                return Err(StorageError::Backend(
                    "object write refused: tenant encryption unavailable".into(),
                ));
            }
            None => body,
        };
        tags.push(system_tag(TAG_TENANT, s.tenant));
        tags.push(system_tag(TAG_COMMUNITY, s.community));
        tags.push(system_tag(TAG_REQUEST_ID, &s.request_id));
        self.check_quota(
            &scope_prefix(&r.owner, s.tenant, s.community),
            &r.full_key,
            body.len() as u64,
        )
        .await?;
        // Audit-first: a cross-prefix write that cannot be recorded does not happen.
        self.audit_allowed(s, &r, "put", key).await?;
        self.backend
            .put(&r.full_key, body, content_type, &tags)
            .await?;
        metrics::bytes("in", size);
        metrics::op_duration("put", t0.elapsed());
        Ok(ObjectInfo {
            key: key.into(),
            size,
            content_type: content_type.into(),
        })
    }

    /// Reads one object (tag-filtered).
    pub async fn get(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
    ) -> Result<(ObjectInfo, Bytes), StorageError> {
        let t0 = Instant::now();
        let r = self.resolve(s, owner, Op::Read, key).await?;
        // Filter on the FETCHED copy's own tags (no separate head: no head->get race).
        let obj = self
            .backend
            .get(&r.full_key)
            .await?
            .ok_or(StorageError::NotFound)?;
        if !Self::visible(s, r.cross, &obj.meta.tags) {
            metrics::denied("tag_hidden");
            self.audit_denied(
                s,
                &r.owner,
                "get",
                key,
                AuditOutcome::Denied,
                "object not tagged for caller",
            )
            .await;
            return Err(StorageError::NotFound);
        }
        let tagged_sealed = obj.meta.tags.contains(&system_tag(TAG_SEALED, 1));
        let body = match &self.sealing {
            Some(sealing) => {
                sealing
                    .open(s.tenant, s.community, &r.owner, key, obj.body, &obj.meta)
                    .await?
            }
            None if tagged_sealed || crate::seal::looks_sealed(&obj.body) => {
                return Err(StorageError::Backend(
                    "object is tenant-sealed but sealing is not configured".into(),
                ));
            }
            None => obj.body,
        };
        let mut meta_info = info(key, &obj.meta);
        meta_info.size = body.len() as u64;
        metrics::bytes("out", meta_info.size);
        metrics::op_duration("get", t0.elapsed());
        self.audit_allowed(s, &r, "get", key).await?;
        Ok((meta_info, body))
    }

    /// Metadata of one object (tag-filtered). Needs `read`.
    pub async fn head(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
    ) -> Result<ObjectInfo, StorageError> {
        let r = self.resolve(s, owner, Op::Read, key).await?;
        let m = self.require_visible(s, &r, "head", key).await?;
        self.audit_allowed(s, &r, "head", key).await?;
        Ok(info(key, &m))
    }

    /// Lists objects under `key_prefix` (tag-filtered); `cursor` is opaque.
    pub async fn list(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key_prefix: &str,
        cursor: Option<&str>,
        limit: u32,
    ) -> Result<ListPage, StorageError> {
        let t0 = Instant::now();
        if !s.enabled {
            metrics::denied("flag_off");
            return Err(StorageError::NotImplemented);
        }
        Self::require_scope(s)?;
        validate_key_prefix(key_prefix)?;
        let owner_s = owner.unwrap_or(&s.app_id).to_string();
        validate_owner(&owner_s)?;
        self.audit_backlog_check(s)?;
        self.rate_check(s)?;
        let cross = match authorize(&s.app_id, &owner_s, Op::List, &s.grants) {
            Ok(c) => c,
            Err(e) => {
                metrics::denied("not_granted");
                self.audit_denied(
                    s,
                    &owner_s,
                    "list",
                    key_prefix,
                    AuditOutcome::Denied,
                    "no approved grant",
                )
                .await;
                return Err(e);
            }
        };
        let scope = scope_prefix(&owner_s, s.tenant, s.community);
        let rel_prefix = key_prefix.strip_suffix('/').unwrap_or(key_prefix);
        let full_prefix = if rel_prefix.is_empty() {
            scope.clone()
        } else {
            format!("{scope}{rel_prefix}/")
        };
        let after = match cursor {
            Some(c) => {
                validate_key(c)?;
                Some(format!("{scope}{c}"))
            }
            None => None,
        };
        let limit = limit.clamp(1, MAX_LIST_LIMIT) as usize;
        let (mut objects, mut after, mut scanned) = (Vec::new(), after, 0usize);
        let mut next = None;
        'outer: loop {
            let batch = self
                .backend
                .list(
                    &full_prefix,
                    after.as_deref(),
                    limit.min(MAX_LIST_SCAN - scanned).max(1),
                )
                .await?;
            if batch.is_empty() {
                break;
            }
            for (full, _) in &batch {
                scanned += 1;
                after = Some(full.clone());
                let Some(meta) = self.backend.head(full).await? else {
                    continue;
                };
                let rel = full.strip_prefix(&scope).unwrap_or(full).to_string();
                if !Self::visible(s, cross, &meta.tags) {
                    continue;
                }
                objects.push(info(&rel, &meta));
                if objects.len() == limit || scanned >= MAX_LIST_SCAN {
                    next = Some(rel);
                    break 'outer;
                }
            }
            if scanned >= MAX_LIST_SCAN {
                next = after
                    .as_deref()
                    .and_then(|a| a.strip_prefix(&scope))
                    .map(str::to_string);
                break;
            }
        }
        if cross {
            self.audit_raw(
                s,
                &owner_s,
                "list",
                key_prefix,
                AuditOutcome::Allowed,
                "approved grant + tag",
            )
            .await?;
        }
        metrics::op_duration("list", t0.elapsed());
        Ok(ListPage {
            objects,
            next_cursor: next,
        })
    }

    /// Deletes one object (tag-filtered; cross-prefix needs `delete`).
    pub async fn delete(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
    ) -> Result<(), StorageError> {
        let r = self.resolve(s, owner, Op::Delete, key).await?;
        self.require_visible(s, &r, "delete", key).await?;
        self.audit_allowed(s, &r, "delete", key).await?;
        self.backend.delete(&r.full_key).await?;
        Ok(())
    }

    /// Short-lived, GET-only presigned URL for ONE object (tag-filtered; needs `read`).
    pub async fn presign_get(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
        ttl_secs: u32,
    ) -> Result<String, StorageError> {
        if ttl_secs == 0 {
            return Err(StorageError::Invalid("ttl must be >= 1".into()));
        }
        let r = self.resolve(s, owner, Op::Read, key).await?;
        let meta = self.require_visible(s, &r, "presign_get", key).await?;
        if meta.tags.contains(&system_tag(TAG_SEALED, 1)) {
            // A presigned URL hands the guest the stored bytes directly: for a tenant-sealed
            // object that is ciphertext. Refuse loudly; callers use `get`.
            return Err(StorageError::Invalid(
                "presigned URLs are unavailable for tenant-sealed objects; use get".into(),
            ));
        }
        let url = self
            .backend
            .presign_get(
                &r.full_key,
                Duration::from_secs(ttl_secs.min(MAX_PRESIGN_TTL_SECS) as u64),
            )
            .await?;
        self.audit_allowed(s, &r, "presign_get", key).await?;
        Ok(url)
    }

    /// OWNER-ONLY: shares own object with `consumer` by adding tag `app-bundle:<consumer>`. Always audited.
    pub async fn tag_add(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
        consumer: &str,
    ) -> Result<(), StorageError> {
        self.tag_change(s, owner, key, consumer, true).await
    }

    /// OWNER-ONLY: revokes `consumer`'s visibility. Always audited.
    pub async fn tag_remove(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
        consumer: &str,
    ) -> Result<(), StorageError> {
        self.tag_change(s, owner, key, consumer, false).await
    }

    async fn tag_change(
        &self,
        s: &StorageScope,
        owner: Option<&str>,
        key: &str,
        consumer: &str,
        add: bool,
    ) -> Result<(), StorageError> {
        let op: &'static str = if add { "tag_add" } else { "tag_remove" };
        if !s.enabled {
            return Err(StorageError::NotImplemented);
        }
        Self::require_scope(s)?;
        validate_key(key)?;
        validate_owner(consumer)?;
        let owner_s = owner.unwrap_or(&s.app_id);
        // Parity with resolve/list: the audited owner string is always validated first.
        validate_owner(owner_s)?;
        self.audit_backlog_check(s)?;
        self.rate_check(s)?;
        if owner_s != s.app_id {
            metrics::denied("tag_not_owner");
            self.audit_denied(
                s,
                owner_s,
                op,
                key,
                AuditOutcome::Denied,
                "tag ops are owner-only",
            )
            .await;
            return Err(StorageError::NotGranted(
                "tag operations are owner-only".into(),
            ));
        }
        let full = object_key(&s.app_id, s.tenant, s.community, key);
        let meta = self
            .backend
            .head(&full)
            .await?
            .ok_or(StorageError::NotFound)?;
        let tag = bundle_tag(consumer);
        validate_tag(&tag)?;
        let mut tags = meta.tags;
        if add {
            if !tags.contains(&tag) {
                tags.push(tag);
            }
            if tags.len() > MAX_TAGS {
                return Err(StorageError::Invalid("too many tags".into()));
            }
        } else {
            tags.retain(|t| *t != tag);
        }
        tags.sort();
        // Audit-first: a tag change that cannot be recorded does not happen.
        self.audit_raw(
            s,
            &s.app_id,
            op,
            key,
            AuditOutcome::Allowed,
            &format!("consumer={consumer}"),
        )
        .await?;
        if !self.backend.set_tags(&full, &tags).await? {
            return Err(StorageError::NotFound);
        }
        Ok(())
    }
}
