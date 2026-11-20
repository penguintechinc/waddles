# Token debit: idempotency, amount bound, and route exposure

## Idempotent debits

`POST /api/v1/internal/communities/{id}/tokens/debit` (machine JWT, `svc-streaming` only) accepts an
optional `idempotency_key` (1-128 chars).

- The key is scoped to community + product and stored on `token_transactions.idempotency_key`
  (alembic `0134_token_debit_idempotency`; partial UNIQUE index, NULL for un-keyed rows).
- A repeat of a key debits nothing and returns the ORIGINAL `balance_after` / `transaction_id`.
- Concurrent same-key requests debit once: the UNIQUE index arbitrates, and the loser rolls back
  its own decrement before returning the original result.
- Un-keyed debits behave exactly as before (no de-duplication).
- A replay returns the original result even if the retried request's `amount` differs.

svc-streaming sends `stream:{community_id}:{config_id}:{admission_id}` (never a timestamp).
`admission_id` is the client's `Idempotency-Key` header on `POST .../configs/{id}/start`
(`[A-Za-z0-9._-]`, max 64 chars); a client MUST reuse the same value when retrying one start
attempt. With no (or an invalid) header a fresh UUID is used, i.e. the request is a new attempt and
is not replay-protected.

## Maximum amount

A single debit is rejected with 400 when `amount` exceeds `TOKEN_DEBIT_MAX_AMOUNT` (hub-api env,
default `10000`; a non-integer or non-positive value falls back to the default). The bound applies
to every `debit_tokens` caller, not only the internal route.

## `/api/v1/internal/*` is not public

The route family is service-to-service only. Defence in depth, beyond machine-JWT verification:

- `admin/hub_module/webui-server/internal-guard.js` makes the public webui proxy answer 404 (and
  forward nothing) for `/api/v{N}/internal[/...]`, case-insensitively, after percent-decoding
  (up to 4 rounds), and for any path with `.`/`..` segments, encoded `/` or `\`, NUL, or malformed
  encoding.
- The Helm chart refuses to render an Ingress path / HTTPRoute rule under `/api/v1/internal`, or a
  covering prefix (`/`, `/api`, `/api/v1`) aimed directly at hub-api.
