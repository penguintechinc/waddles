# Ban-Evasion Suppression -- Legitimate Interest Assessment (LIA)

> **DRAFT -- counsel to verify.** This is a working assessment prepared by engineering for review by
> the privacy lead and counsel. It is **not** a final legal artifact: the conclusions in section 7
> are provisional, the open questions in section 9 are real, and nothing here has been signed off.
> Companion documents: [Suppression DPIA (stub)](suppression-DPIA.md) and the user-facing
> disclosure in [`docs/PRIVACY.md`](../PRIVACY.md) (*What Is Retained*, section 1).

| | |
|---|---|
| Processing | Retaining a keyed one-way code of a banned user's platform id after that user erases their account, so the ban is not evaded by erase-and-return |
| Lawful basis | Art. 6(1)(f) legitimate interest -- **not** an Art. 17(3) exemption |
| Data | `suppression_records`: tenant, platform name, key version, HMAC, restriction type (`ban`/`auto_ban`), expiry, created-at |
| Not kept | platform id, username, e-mail, display name, hub user id, community, offence, free text, moderator |
| Data subjects | Users whose account is erased while a `community_members` row of theirs has `role = 'banned'` |
| Roles (proposed) | Tenant = controller of its own ban list; Waddles = processor (Art. 28). **Open question 1** |
| Scope | Per tenant. A code never matches outside the tenant that wrote it |
| Code / tests | `hub_api/services/ban_suppression.py`, `suppression_keys.py`, `data_privacy_service.py`; migration `0060_suppression_records` |

## 1. What changed and why

Erasure (GDPR Art. 17) previously kept, for **every** erased user, three things: the platform
account links (`hub_user_identities`), the reputation score + audit trail
(`reputation_tenant`, `reputation_events`) and the membership rows with only their personal
columns nulled (`community_members`). That was justified as an anti-gaming signal ("farm score,
delete, repeat") -- a retention applied to everyone to catch the few.

The replacement is **suppression only**:

| Erased user has... | Kept | Returns later |
|---|---|---|
| no active ban | **nothing** -- no reputation, no score, no identity link, no tombstone | fresh account, fresh reputation |
| an active ban | one `suppression_records` row per (tenant, platform account) | matched by recomputing the code; the restriction is re-applied in that tenant |

This reduces retention from "every erased user" to "erased users with an active ban", and from
"raw platform id + score" to "a keyed code + a restriction type".

## 2. Purpose test -- is there a legitimate interest?

* **Interest.** A community (the tenant) bans a user to protect its members from abuse and to
  enforce its rules. If erasure plus re-registration erased the ban, the ban would be unenforceable
  against exactly the users it was imposed on: the right to erasure would double as an
  evasion tool.
* **Whose.** The tenant's (and its members'), as controller. Waddles' own interest is derivative:
  operating a moderation capability its customers expect.
* **Lawful and real.** Community moderation is lawful and ordinary; the interest is current (the
  ban exists now), not speculative.
* **Not an Art. 17(3) exemption.** Art. 17(3)(e) (legal claims) was considered and **not** relied
  on: a ban is not a pending or likely claim. Retention rests on Art. 6(1)(f) alone and must
  therefore pass the necessity and balancing tests below.

## 3. Necessity test -- is retention necessary, and is this the least intrusive form?

| Option | Verdict | Reason |
|---|---|---|
| Keep the full account, identity links and reputation (the previous design) | Rejected | Over-retention for **all** erased users to catch a few; keeps raw platform ids |
| Keep nothing | Rejected | The ban is unenforceable; erasure becomes an evasion tool |
| Keep the raw platform id | Rejected | Directly identifying, reversible, linkable across tables |
| Keep an unkeyed / statically-salted hash of the platform id | Rejected | A Discord/Twitch id is a short numeric string: the hash is a lookup table an attacker enumerates in seconds (`test_suppression_keys.py::TestNotReversibleWithoutTheKey`) |
| **Per-tenant keyed HMAC + closed-enum type + expiry** | **Chosen** | Deterministic (a return matches) yet useless without `K_master`; per-tenant so no cross-tenant list; carries no offence detail |
| Rely on the platform-side ban (Discord/Twitch) alone | Complementary, not sufficient | A platform ban does not stop the person from regaining Waddles-local standing (membership, reputation, roles, hub features) with a new account |

**Weakest link, stated plainly.** Where the platform-side ban already prevents the harm, the
*additional* necessity of a Waddles-local record is weaker. Counsel should decide whether
suppression should be limited to restrictions the platform cannot enforce (**open question 4**).

Data minimisation as built: only the code, the platform *name*, the key version, a two-value
type and an expiry. No community id is stored (so the row cannot be tied to a specific community or
incident), no reason text, no moderator.

## 4. Balancing test -- do the data subject's interests override?

**Data subject's interests.** They exercised a statutory right (erasure) and reasonably expect to
be forgotten; being on a ban list is stigmatising; an erroneous or stale ban would persist.

**Nature of the data.** A pseudonymised code is still personal data (Recital 26): the controller
can re-identify by recomputing the code from a known platform id. The restriction type is the only
substantive attribute, and it is a closed vocabulary with no offence description: community-rule
sanctions are not criminal-offence data (Art. 10) and no special-category data (Art. 9) is stored.
**Open question 5** asks counsel to confirm that a bare "banned" flag cannot become Art. 9/10 data
in a given tenant's context (e.g. a ban that implies a protected characteristic).

**Reasonable expectations.** Before confirming deletion the user is told, in the privacy-rights
page and in `PRIVACY.md`, that a coded ban record is kept until it expires or is lifted, and that
they may object. The disclosure is in the product, not buried.

**Likely impact.** Low volume, low sensitivity, no profiling. The consequence for the person is
continued exclusion from the community that banned them -- the intended effect of the ban itself.

**Safeguards (each is implemented and tested):**

| Safeguard | Where |
|---|---|
| Keyed, not salted; key in `hub-api` only, never in the DB | `suppression_keys.py`; chart mounts `TENANT_KEK_HEX` into hub-api only (`test_suppression_hub_api_env_render.py`) |
| Per tenant: HKDF per-tenant key, tenant-scoped queries, platform bound into the MAC | `test_ban_suppression.py::TestReturnRecatch` |
| Closed restriction vocabulary (DB CHECK), no free text | migration 0060 + `alembic/tests/test_0060_suppression_records.py::TestClosedVocabulary` |
| Written only for an *active ban*; benign erasure keeps nothing and never touches the key | `TestErasureBranching` |
| Same transaction as erasure; a missing key aborts the whole erasure (no silent un-suppressed erasure) | `TestFailLoud` |
| Expiry honoured at lookup even before the purge job runs; hourly purge; operator `purge` | `TestReturnRecatch`, `TestPurgeLoop`, `cli/suppression.py` |
| Optional retention ceiling (`suppression.maxRetentionDays`) for storage limitation | `TestFailLoud::test_retention_ceiling_caps_an_indefinite_ban` |
| Lift on a moderator decision or an upheld objection; read-only `check` for access requests | `cli/suppression.py` (`lift`, `check`) |
| No platform id, code or e-mail in logs, spans or metric labels | `TestObservability` |
| Disclosed to the subject in their Art. 15 export (`suppression_records_written` in the deletion ledger) | `data_deletion_requests.deletion_scope` |
| Fail closed: a live row + an unavailable key refuses admission rather than letting a banned user in | `TestReturnRecatch::test_a_lookup_with_live_rows_but_no_key_fails_closed` |

## 5. Retention

* **Default:** until the restriction is lifted or its expiry passes (a NULL expiry means "until
  lifted"). Today the only persisted ban state (`community_members.role = 'banned'`) has **no**
  expiry, so every row is currently indefinite.
* **Why that is the weakest point.** Indefinite retention of even a pseudonymised code is hard to
  defend under Art. 5(1)(e). The chart value `suppression.maxRetentionDays` (1-3650) caps the life
  of every row when it is written. **Recommendation:** counsel sets a ceiling (a 12-24 month
  starting range is suggested for discussion, not a legal conclusion) and operations sets it.
* **Rotation.** `TENANT_KEK_HEX_PREVIOUS` must stay set until no live row carries the old
  `key_ver` (`make suppression-stats` shows the breakdown); dropping it earlier makes those rows
  unmatchable -- a documented fail-open for the oldest bans, not a silent one.

## 6. Data subject rights

| Right | How it is met |
|---|---|
| Information (Art. 13/14) | `PRIVACY.md` (*What Is Retained*, section 1), the privacy-rights page copy, this LIA |
| Access (Art. 15) | `make suppression-check` (needs the subject's platform id, supplied by them) answers "is there a live row for me in tenant T"; the deletion ledger discloses that one was written. **Process** for verifying the requester's control of the platform id is open (question 6) |
| Rectification (Art. 16) | n/a -- the record holds no mutable personal attribute |
| Erasure (Art. 17) | The row is erased at expiry, on lift, or when the tenant is deleted (`ON DELETE CASCADE`). A request to erase the record itself is an objection (below) |
| Objection (Art. 21) | A human reviews; if upheld, `make suppression-lift` deletes the row at once. **The review process is not built** (section 8) |
| Automated decisions (Art. 22) | `auto_ban` rows come from the reputation policy enforcer's automatic ban. Human review of automated bans is **not built** (section 8) |

## 7. Provisional outcome

On the facts above the assessment is **provisionally favourable** to retaining the keyed code for
users with an active ban, **conditional on**: (a) counsel confirming the controller/processor
allocation; (b) a retention ceiling being chosen and set; (c) the appeal / human-review process of
section 8 existing before this is enabled for a tenant; (d) the DPIA being completed. If counsel
concludes the necessity case is too weak where a platform-side ban exists, the fallback is to
restrict suppression to restrictions the platform does not itself enforce.

## 8. Process follow-ups (NOT built in this change -- flagged for owners)

| # | Item | Why it is needed |
|---|---|---|
| P1 | **DSA statement of reasons and appeal.** Where a tenant acts as an online platform restricting a user's account, DSA Art. 17 requires a statement of reasons and Art. 20 an internal complaint-handling route | A ban re-applied on return is a restriction of the user's account |
| P2 | **Art. 22 human review for automated bans** (`auto_ban`). A documented path to a human decision, with the outcome able to lift the row | The reputation policy enforcer bans automatically when a score crosses a threshold |
| P3 | **Requester verification** for access/objection requests that name a platform id | The code is only testable with the id; the requester must prove they control it |
| P4 | **A moderator-facing lift UI/API.** Lifting is operator-only (`cli/suppression.py`); a tenant admin cannot do it themselves yet. Adding an admin route also means regenerating `openapi/v1.yaml` (a merge hot-spot with other open PRs) | Right to object / appeal should not need an operator |
| P5 | **Reconciliation sweep.** `core/reputation_module` still creates platform-keyed member rows on a chat event and cannot be given the master key (it must stay inside hub-api); it needs an authenticated hub-api check or a periodic hub-api sweep. Until then a returning banned user is re-caught at join / signup / identity-link, not at the first chat message | Closes the remaining re-entry route |
| P6 | **Timed bans.** Waddles persists no ban expiry today (`expires_at` is NULL for every row); platform-side timeouts are not stored, so there is nothing to carry. If timed bans are persisted, map them to `expires_at` | Storage limitation |

## 9. Open questions for counsel

1. Controller / processor allocation for the suppression list. Is "tenant = controller, Waddles =
   processor" correct for a list Waddles itself computes and purges? Does the DPA need a clause?
2. Is legitimate interest available to the *tenant* for retention beyond erasure, given the
   data subject's Art. 17 request is directed at the platform?
3. Is a tenant-wide scope proportionate for a ban imposed in a single community? (The row carries
   no community id by design; a community-scoped variant is possible at the cost of an extra
   attribute.)
4. Should suppression be limited to restrictions the platform cannot enforce (section 3)?
5. Can a bare "banned" flag ever be Art. 9/10 data in a tenant's context?
6. What proof of control over a platform id is adequate for access and objection requests?
7. Retention ceiling: what period is defensible, and should it differ for `auto_ban` vs `ban`?
8. Does an `auto_ban` meet the "similarly significantly affects" threshold of Art. 22(1)?
9. Cross-border: the key lives in the same region as the database; any transfer concern?

## 10. Verification

Each safeguard above is pinned by a test. The mutation-verification record (each production-code
mutation applied in-process, the suites re-run, then reverted) is in section 11. Test totals and
coverage are in the pull request description.

## 11. Fail-first (mutation) record

Executed 2026-10-10, not narrated: each mutation below was applied to the production source
in-process, the relevant suites re-run, then the file restored. **54 of 54 mutants were killed**
(one -- the transaction helper never rolling back -- first survived and drove a new direct test,
`TestTransactionHelper`). A mutant counts as killed on the first red test.

| Property | Mutants (each turned the suite red) |
|---|---|
| Erasure is complete (nothing kept for a benign user) | A1 `reputation_events` not deleted - A2 `reputation_tenant` - A3/A4 bundle reputation scores / ledger - A5 pseudonym mapping - A6 `hub_user_identities` kept as an anchor - A7 member rows kept - A12 hub-uuid dropped from the bundle-reputation key set - A10 `member_count` not decremented |
| Suppression only for an active ban | A8 restrictions dropped (no row) - B1 any role counts as banned - B18/B19 wrong identity set suppressed (hub-linked vs platform-keyed) - B17 tenant-less community still coded |
| Benign erasure never touches config/key | A11 retention ceiling read for every user - B4 key read before the no-live-rows check |
| One transaction, all-or-nothing, fail loud | A13 suppression row committed on its own - A14 no rollback on failure - B15 transaction helper never rolls back |
| Per tenant only | B2 lookup not tenant-scoped - B14 re-apply bans every tenant - C1 tenant key independent of tenant id |
| Expiry / purge / lift | B3 expiry ignored - B5 purge deletes nothing - B6 purge deletes live rows - B7 lift does not delete - B9 earlier expiry wins - B10/A9 retention ceiling ignored |
| Keyed, versioned, stable code (over the envelope KEK seam) | C2 unkeyed SHA-256 - C3 platform not bound - C4 platform not lower-cased - C5 only the current key tried - C6 static HKDF salt - C7 previous KEK not listed for rotation - C8 unknown KEK id falls back to the current KEK - C9 id case folded - C10 HKDF label ignored (all uses share a key) - C11 KEK-load failure swallowed into a zero key |
| Classification and upsert | B8 BAN no longer outranks AUTO_BAN - B12 `auto_ban` never classified - B16 duplicate row instead of extending |
| Re-application on return | B11 re-applied membership stays active - B13 match found but not banned - D1 join approval skips the gate - D2/D3 OAuth link (new + retry) - D4 OAuth signup - D5/D6 identity link (new + retry) |
| Operator CLI | E1 `lift` always exits 0 - E2 `check` exits 0 on no match - E3 `stats` counts expired as live - E4 id accepted off the stdin path |

## 12. Sign-off (placeholder)

| Role | Name | Date | Decision |
|---|---|---|---|
| Privacy lead | _pending_ | | |
| Counsel | _pending_ | | |
| Engineering owner | _pending_ | | |
