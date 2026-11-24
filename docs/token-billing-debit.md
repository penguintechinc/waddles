# Token debit: idempotency, amount bound, and route exposure

## Idempotent debits (retry guard, not a coupon)

`POST /api/v1/internal/communities/{public_uuid}/tokens/debit` (machine JWT, `svc-streaming` only) accepts an
optional `idempotency_key` (1-128 chars). It identifies ONE admission attempt.

- Stored on `token_transactions.idempotency_key` (scoped to community + product) with the
  `actor_user_id` it was for (alembic `0135_token_debit_idempotency`; partial UNIQUE index).
- Same key, same actor, same amount, within the replay window: nothing is debited and the ORIGINAL
  `balance_after` / `transaction_id` is returned. Concurrent same-key requests debit once (the
  UNIQUE index arbitrates; the loser rolls back its own decrement).
- Same key from a different actor, with a different amount, or older than the window: **409**
  `idempotency_key_conflict`, nothing debited, nothing replayed.
- Window: `TOKEN_DEBIT_IDEMPOTENCY_TTL_SECONDS` (default `600`; non-integer or non-positive falls
  back to the default).
- Un-keyed debits behave as before (no de-duplication).

### svc-streaming key derivation

`stream:{community_id}:{config_id}:{actor_user_id}:{run_session_id}`. The run-session id is
allocated by svc-streaming itself the first time an actor starts a config and reused for retries of
that start; the client's `Idempotency-Key` header is NOT used (a client reusing one key on every
start would otherwise get free replays). The key is retired as soon as the pipeline has started (a
later start after a stop is a new admission and debits again), on stop, and when hub-api answers
409. A retry of a start whose pipeline is already running is neither admitted nor billed. Sessions
are held in process memory; a svc-streaming restart between a debit and its retry bills the retry
as a new admission.

## Maximum amount

A single debit is rejected with 400 when `amount` exceeds `TOKEN_DEBIT_MAX_AMOUNT` (hub-api env,
default `10000`; a non-integer or non-positive value falls back to the default). It applies to
every `debit_tokens` caller, not only the internal route.

## `/api/v1/internal/*` is not public

The route family is service-to-service only. Defence in depth, beyond machine-JWT verification:

- `admin/hub_module/webui-server/internal-guard.js` makes the public webui proxy answer 404 (and
  forward nothing) for `/api/v{N}/internal[/...]`, case-insensitively, after percent-decoding
  (up to 4 rounds), and for any path with `.`/`..` segments, encoded `/` or `\`, NUL, or malformed
  encoding.
- The Helm chart refuses to render an Ingress path / HTTPRoute rule under `/api/v1/internal`, or a
  covering prefix (`/`, `/api`, `/api/v1`) aimed directly at hub-api.
