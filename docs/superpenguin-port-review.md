# SuperPenguinTV port review — 2026-11-07

Scope: C# balance/credit/debit service client and persisted Duel money plans.
Wagering/max-bet remain app-owned; the economy service is unchanged.
The user explicitly approved Gemini and local-network Ollama review.

| Reviewer | Finding | Verified disposition |
|---|---|---|
| Gemini | Abort cleanup releases ownership before persisting terminal state | Fixed: persist the aborted plan first; crash-injection test proves retry cannot revive it. Failed cleanup can leave claims until their existing TTL; this is documented. |
| Gemini | Broker retry changes message ID and causes double debit | Does not match runtime: `spine.rs` anchors identity to the signed envelope event ID, explicitly stable across redeliveries. Found a separate real gap: context omitted that ID. The host-context fix forwards it, with a spoof/retry regression test. |
| Gemini | Two-debit plan could partially debit two players | That operation list is not generated: each command debits only its own actor. Actual later-credit failures still fail loudly and retry; Duel's nontransactional crash/recovery limitations remain documented, not declared solved. |
| Gemini | Delimiter injection / small noncryptographic hash collisions | Inputs here are host event IDs, internal plan keys/operation literals and canonical UUIDs. The hash is two 64-bit passes, not a 32-bit hash and not an authorization primitive. Keep this constraint; do not expose arbitrary operation IDs to chat arguments. |
| Gemini | Unmapped malformed service responses throw tool exceptions | Intentional fail-loud behavior. Only actionable insufficient-funds/over-cap refusals may abort a money plan; corruption/unknown failures must not be treated as successful or trigger unsafe compensation. Tests cover malformed results. |
| Gemini | Case-sensitive user comparison | Identity host returns canonical UUID strings; usernames are not accepted. A mismatched returned user fails closed. |
| Ollama | No review result | Configured LAN endpoint timed out. Do not describe this as approval or a successful independent review. |

This was a focused review, not a release security audit or proof of live database
atomicity. The host-context addition needs normal PR/CI review. Local game/SDK
coverage, WASM builds, executor binding tests and release gate status are recorded
in root `.TODO` / `.PLAN`. No automatic review finding is silently treated as an
approval; assumptions and residual limits remain visible here.

## Gemini follow-up

With the real signed-envelope contract and abort fix, Gemini confirmed that
exposing the event ID does not widen authority and retries do not duplicate debits.
It separately retained the documented claim/payout availability limitations.
It proposed putting amount in the retry key; that suggestion was not applied:
`bundle_host_economy` compares the full original operation parameters and returns
`IdempotencyConflict` for mismatches. Changing the key for a changed amount would
allow a second mutation instead of rejecting a corrupted retry. A C# regression
test pins the stable-key/conflict contract. Canonical UUID and fail-loud error
handling assumptions remain as documented above. This is still a focused review,
not full release signoff.
