# Ban-Evasion Suppression -- Data Protection Impact Assessment (DPIA) STUB

> **STUB -- counsel to verify.** This skeleton records what engineering already knows and what the
> privacy lead / DPO must still decide. It is **not** a completed DPIA and must not be filed as
> one. Read with the [Legitimate Interest Assessment](suppression-LIA.md).

## 1. Is a DPIA required?

Art. 35 needs a DPIA where processing is likely to result in a high risk. Factors that point
**towards** one: monitoring/enforcement against individuals, a list of sanctioned users, partly
automated decisions (`auto_ban`), persistence after an erasure request. Factors **against**:
tiny data set (a keyed code, an enum, an expiry), no profiling, no special-category data stored,
per-tenant scope, short-lived by design. **Provisional answer: carry out a (short) DPIA** -- the
persistence-after-erasure and the automated-ban element are enough to justify the paperwork.
_Decision owner: DPO. Status: pending._

## 2. Description of the processing

| | |
|---|---|
| Nature | When an account is erased while it holds an active ban, compute `HMAC-SHA256(HKDF(K_master, tenant_id), platform || platform_user_id)` and store it with the platform name, key version, restriction type and expiry; on a later join / signup / identity link, recompute and compare within the same tenant |
| Scope | Users with an active ban at the moment of erasure; one row per (tenant, platform account); volume = number of such users |
| Context | Community moderation inside a customer's tenant (the controller); Waddles operates the system (processor) |
| Purpose | Prevent evasion of a community ban by erase-and-return |
| Recipients | None outside `hub-api`; the key is held only by `hub-api`; no other service role can read the table |
| Retention | Until lifted or expired; optional operator ceiling `suppression.maxRetentionDays` (unset by default) |
| Systems | `hub-api` (Postgres `suppression_records`, migration 0060); Helm chart `suppression:` values |

Data flow:

```
erase request ──► _sync_erase (one transaction)
                    ├─ active ban?  no ──► delete everything incl. reputation   (nothing kept)
                    └─ active ban?  yes ─► suppression_records += keyed code ──► delete everything
return (join / signup / identity link) ──► recompute keyed code in the DESTINATION tenant
                    └─ live match ──► membership written banned + inactive
```

## 3. Necessity and proportionality

See the LIA (sections 2-4). Summary: lawful basis Art. 6(1)(f); least-intrusive option chosen
from six considered; data minimised to code + type + expiry; per-tenant; expiry and lift
supported. **Open:** retention ceiling, requester verification, Art. 22 review, DSA appeal
(LIA sections 8-9).

## 4. Risks to individuals

Likelihood and severity are engineering estimates, to be re-rated by the DPO.

| # | Risk | Likelihood | Severity | Mitigations in place | Residual |
|---|---|---|---|---|---|
| R1 | Database leak lets an attacker learn who is banned | Low | Medium | Keyed HMAC (key not in DB), per-tenant key, no platform id stored, hub-api-only privileges (RBAC matrix) | Low |
| R2 | Master-key compromise lets an attacker test guessed ids per tenant | Very low | Medium | Key only in hub-api, never logged, never in DB; PII-free logs; rotation via `key_ver` + `TENANT_KEK_HEX_PREVIOUS` | Low |
| R3 | Cross-tenant tracking (a global ban list) | Low | High | Per-tenant derived key, tenant-scoped queries, tenant FK cascade; tested to never cross-match | Very low |
| R4 | Erroneous or stale ban persists indefinitely | Medium | Medium | Expiry, optional ceiling, purge, lift; **appeal process not yet built** | **Medium until P1/P2/P4 exist** |
| R5 | Unfair automated ban becomes sticky | Medium | Medium | `auto_ban` distinguished from `ban` for review; **human review not yet built** | **Medium until P2 exists** |
| R6 | A ban implies special-category / offence data | Low | High | Closed enum, no offence text, no reason; counsel to confirm (LIA Q5) | Low-Medium pending Q5 |
| R7 | Subject cannot exercise access / objection rights | Medium | Medium | `check` / `lift` operator tooling; **verification process not built** (P3) | Medium until P3 exists |
| R8 | Key misconfiguration silently disables suppression | Low | Medium | Fail loud: banned erasure aborts without the key; a live row + missing key fails admission closed; chart test pins the mount | Low |
| R9 | Retention longer than needed | Medium | Low | NULL = until lifted is the default; ceiling recommended | Low once a ceiling is set |

## 5. Consultation

* Data subjects / representatives: _not consulted_ (n/a for an enforcement control; the
  user-facing disclosure is in the product and `PRIVACY.md`).
* Tenant controllers: _to be consulted via the DPA addendum_ (LIA Q1).
* DPO / supervisory authority prior consultation (Art. 36): _only if residual high risk remains
  after the open mitigations are in place._

## 6. Measures that remain to be completed before enabling for a tenant

- [ ] Counsel confirms the controller/processor allocation and the lawful basis (LIA Q1-Q2).
- [ ] Retention ceiling chosen and `suppression.maxRetentionDays` set (LIA Q7).
- [ ] Appeal / human-review process live (LIA P1, P2) and requester verification defined (P3).
- [ ] Moderator-facing lift route (P4) or an agreed operator SLA for lifts.
- [ ] Reconciliation sweep or authenticated check for `reputation_module`-created rows (P5).
- [ ] This DPIA completed, signed and reviewed on a schedule (suggest annually).

## 7. Sign-off (placeholder)

| Role | Name | Date | Decision |
|---|---|---|---|
| DPO | _pending_ | | |
| Counsel | _pending_ | | |
| Engineering owner | _pending_ | | |
