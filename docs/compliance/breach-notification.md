# Personal-Data Breach Notification Runbook

> **Status: DRAFT - pending legal/DPO review.** Not legal advice. Items in `[PLACEHOLDER]`
> must be filled by the operator before this runbook is relied on. Closes GRC finding G6.

**Roles.** Per `docs/PRIVACY.md`, tenants (customer organisations) are the **controllers**; the
Waddles operator is the **processor** for chat/identity data (and controller for its own account,
billing and website data). Both duties below apply.

| Duty | Who -> whom | Deadline |
|---|---|---|
| GDPR Art.33(1) | Controller -> supervisory authority | without undue delay, **<= 72h** after becoming aware, unless unlikely to risk individuals |
| GDPR Art.33(2) | Processor -> controller (tenant) | **without undue delay** (internal target **<= 24h** from awareness) |
| GDPR Art.34 | Controller -> affected individuals | without undue delay, when **high risk** |
| CCPA/CPRA (Cal. Civ. Code 1798.82) | Business -> affected CA residents / AG when >500 | most expedient time possible |

"Aware" = reasonable certainty a security incident compromised personal data, not first alert.

## Contacts (fill before use)

| Role | Contact |
|---|---|
| Incident Commander (IC) | `[PLACEHOLDER: name/rota, pager]` |
| Data Protection Officer / privacy lead | `[PLACEHOLDER: DPO name, email, phone]` |
| Legal counsel | `[PLACEHOLDER]` |
| Supervisory authority (lead) | `[PLACEHOLDER: authority + portal URL]` |
| Security intake | security@waddlebot.io (see `docs/SECURITY.md`) |
| Tenant notification channel | tenant admin contact on the `tenants` record + status page `[PLACEHOLDER]` |

## Severity triage

| Sev | Definition | Examples | Clock |
|---|---|---|---|
| S1 | Confirmed exfiltration/loss of personal data, or credentials/keys/tokens exposed | DB dump leak, tenant isolation bypass reading another tenant's rows, signing/KMS key leak, platform OAuth tokens leaked | notify tenants <= 24h; DPO assesses 72h filing immediately |
| S2 | Probable exposure, scope unconfirmed | Anomalous bulk reads, public bucket misconfig with unknown access | investigate <= 24h; escalate to S1 on evidence |
| S3 | Security event, no personal-data exposure | Blocked attack, vuln without exploitation | normal vuln process |

Risk-to-individuals factors: data type (chat text, handles, IP, auth data), volume, ease of
identification (data is UUID-tokenized outside hub-api, which lowers risk when only tokens leak),
encryption state of leaked data, special-category content in free-text chat.

## Procedure

1. **Detect and declare (T+0).** Anyone reports to security@waddlebot.io / IC. IC opens an incident
   record and starts the **awareness clock** (record UTC timestamp).
2. **Contain (T+0..4h).** Revoke/rotate affected credentials (JWT keys, service tokens, platform
   tokens, KMS), isolate workloads, preserve evidence (logs, tamper-evident `audit_events` chain,
   cluster state). Do not wipe.
3. **Assess (T+0..24h).** Scope: tenants, communities, tables/columns (use the erasure registry
   `hub_api/services/erasure_registry.py` as the data map), record counts, time window, data
   categories. Use `audit_events` and structured logs. **Logs/tickets stay PII-free** (UUIDs only).
4. **Notify tenants - processor duty (<= 24h target, always before their 72h expires).** DPO sends
   each affected tenant: nature of breach, categories + approx. numbers, likely consequences,
   measures taken/proposed, our contact. Phase information if incomplete (Art.33(4)).
5. **Controller filing - tenant-led.** Tenant decides on authority/individual notification; we supply
   data and a draft. For Waddles-as-controller data, DPO files with `[PLACEHOLDER authority]`
   within 72h and decides on Art.34 individual notice with legal.
6. **Individuals (high risk).** Plain-language notice via the affected platform channel/email;
   include what happened, what we did, what they should do, contact.
7. **Remediate and review (<= 14 days).** Root cause, fix, regression test (house rule: every
   incident gets a test), update this runbook. Late filings must record the reason for delay.

## Who does what

| Step | IC | Security eng | DPO/Legal | Comms/Support |
|---|---|---|---|---|
| Declare + clock | owns | assists | informed | - |
| Contain/evidence | decides | executes | - | - |
| Scope assessment | owns | executes | reviews | - |
| Tenant notice | approves | provides facts | drafts/approves | sends |
| Authority filing | informed | provides facts | owns | - |
| Individual notice | approves | facts | owns wording | sends |
| Post-incident review | owns | executes | reviews | - |

## Breach register (Art.33(5))

Every breach, including non-notified ones, is logged with facts, effects, remedial action.
`[PLACEHOLDER: location of register]`. Retain >= `[PLACEHOLDER: e.g. 5 years]`.

## Open items for legal/DPO

- Confirm controller/processor split and a DPA clause binding the 24h tenant-notice target.
- Appoint DPO (or document why not required) and fill contacts.
- Confirm non-EU regimes triggered by the customer base and their deadlines.
