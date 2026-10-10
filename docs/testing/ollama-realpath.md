# Real-endpoint Ollama integration tests (WaddleAI router, AI Researcher, AI Interaction)

Env-gated tests that drive the AI code paths against a **live local Ollama** with no mocked
transport. They test the **integration** -- request shape, response parsing, gate verdicts being
acted on, errors/timeouts, PII redaction -- **not the model**. The lab models are the smallest
available; nothing asserts "input X -> content Y".

| Suite | Code under test | File |
|---|---|---|
| hub-api router + provider clients | `hub_api/services/ai_routing/` (`OllamaClient`, `route_completion`, OpenAI/Anthropic BYOK clients, `pii_redaction`) | `hub_api/tests/test_ai_routing_ollama_realpath.py` |
| AI Researcher | `core/ai_researcher_module` (`AIProviderService`, `ResearchService` + `SafetyLayer`, `SummaryService`) | `core/ai_researcher_module/tests/test_ai_researcher_ollama_realpath.py` |
| AI Interaction | `action/interactive/ai_interaction_module` (`OllamaProvider`, `WaddleAIProvider`, `AIService`) | `action/interactive/ai_interaction_module/tests/test_ai_interaction_ollama_realpath.py` |

## Run

The gate is `WADDLE_TEST_OLLAMA_URL`. **Unset (CI) -> every real-endpoint test SKIPS. Set but
unreachable -> the session FAILS** (a test that skips after you asked for it to run is a gate that
cannot fail). The endpoint is LAN-only, so run from a machine that can reach it.

```bash
export WADDLE_TEST_OLLAMA_URL=http://192.168.2.105:11434/

# one suite at a time -- never several in parallel (see "One query at a time")
(cd hub_api && python3 -m pytest tests/test_ai_routing_ollama_realpath.py -v -rs)
(cd core/ai_researcher_module && python3 -m pytest tests/test_ai_researcher_ollama_realpath.py -v -rs)
(cd action/interactive/ai_interaction_module && python3 -m pytest tests/test_ai_interaction_ollama_realpath.py -v -rs)

# or just the marked tests inside any one module directory
python3 -m pytest -m ollama_realpath -v -rs

# or all three, strictly sequentially (each module's test deps must be installed)
make test-ollama-realpath WADDLE_TEST_OLLAMA_URL=http://192.168.2.105:11434/
```

`-rs` prints the skip reasons. All three suites together are 37 tests and send about 37 requests
(roughly 22 generations, mostly 16-128 tokens; the rest are metadata GETs or instant 404s); the three
runs take well under two minutes on an idle box.

| Env var | Default | Meaning |
|---|---|---|
| `WADDLE_TEST_OLLAMA_URL` | unset | Lab Ollama base URL; unset = skip |
| `WADDLE_TEST_OLLAMA_TEXT_MODEL` | `gemma4:e2b` | Free-tier baseline: **text-only** (no JSON / structured output). Must be pulled -> FAIL if missing |
| `WADDLE_TEST_OLLAMA_JSON_MODEL` | `gemma4:e4b` | First model that handles JSON. Not pulled -> those tests skip |
| `WADDLE_TEST_OLLAMA_SAFETY_MODEL` | `shieldgemma` | Safety / content-classification model. Not pulled -> skip |

These defaults are **test lab config, not the product's tier->model map** (that is a product
decision in progress; see below).

## One query at a time (hard limit)

The lab Ollama shares one GPU with the live WaddleAI. `tests/support/ollama_realpath.py`'s
`SingleFlightGuard` (fixture `single_flight`, used by every real-endpoint test) enforces:

* **Serialization** -- all real `httpx.AsyncHTTPTransport` traffic goes through one cross-process
  `flock`, held from request-send until the response body is closed. Concurrent callers queue; a
  second request is never on the wire, even across pytest-xdist workers or two shells.
* **Host allow-list** -- any request to a host other than the configured Ollama raises
  `SingleFlightViolation` (so a synthetic key can never be sent to a real OpenAI/Anthropic URL).
* **Request budget** -- at most 12 requests per test; a runaway loop cannot hammer the GPU.
* **Teardown assertions** -- the fixture fails the test if it ever observed overlap or a leaked lock.

The guard itself is unit-tested offline (`hub_api/tests/test_ollama_realpath_support.py`).
Timeout / refused / HTTP-500 / garbage-body / dropped-connection handling is tested against a
misbehaving **loopback** server (`hub_api/tests/test_ai_routing_ollama_faults.py`) -- real sockets,
no GPU, runs in CI.

## What is asserted (and what deliberately is not)

* **Well-formed request** on the wire: path, model, `stream:false`, `think:false`,
  `options.{temperature,num_predict}`, role-separated messages, `<user_input>` delimiting, auth
  headers, and **no `format`** for a text-only model.
* **Parsed response**: non-empty `str`, provider/model/tier fields, input/output token counts > 0,
  `json_mode` correct, typed errors (`AI_PROVIDER_ERROR`, `EmptyCompletionError`, ...).
* **Verdicts consumed**: the router's flag/kill-switch/entitlement refusals and the Researcher's
  `SafetyLayer` verdicts result in **zero** requests reaching the model; premium metering debits
  exactly the usage numbers the provider reported; ambient fallback serves the *free* tier's
  capability.
* **Errors**: unknown model -> real 404 -> typed error (no retry storm, endpoint URL not leaked).
* **PII redaction before the LLM call**: the BYOK `OpenAIClient` / `AnthropicClient` are pointed
  (class-level `BASE_URL`) at Ollama's OpenAI-/Anthropic-compatible `/v1`; the recorded wire body
  must contain `[REDACTED_EMAIL]` / `[REDACTED_TOKEN]` and none of the synthetic email / `sk-` /
  `wa-` / Bearer / JWT strings. (The self-hosted free/premium Ollama tiers intentionally do not
  redact -- `pii_redaction` is an egress-boundary control for third-party APIs.)
* **Not asserted**: answer content, classification verdicts, JSON quality. Where a weak model may
  legitimately produce unparseable JSON the test accepts the client's typed, loud failure.

All prompts are synthetic. Secret-shaped strings are assembled at runtime so secret scanners don't
flag the test source.

## Capability-aware model selection (text-only vs JSON)

`gemma4:e2b` (the free-tier default) is **text-only**; `gemma4:e4b` and up handle JSON. Capability
is **configuration**, never inferred from a model name and never a hardcoded tier->model map:

| Where | Env (all optional) | Default | Effect |
|---|---|---|---|
| hub-api free tier | `OLLAMA_URL`, `AI_FREE_MODEL`, **`AI_FREE_SUPPORTS_JSON`**, `AI_FREE_DISABLE_THINKING` | model unchanged; `false`, `true` | text path unless the operator declares JSON support |
| hub-api premium tier | `OLLAMA_PREMIUM_URL`, `AI_PREMIUM_MODEL`, **`AI_PREMIUM_SUPPORTS_JSON`**, `AI_PREMIUM_DISABLE_THINKING` | model unchanged; `false`, `true` | same, per tier |
| AI Researcher | `OLLAMA_MODEL`, **`OLLAMA_SUPPORTS_JSON`**, `OLLAMA_DISABLE_THINKING` | `false`, `true` | summaries use plain labeled-text prompts/parsers unless JSON is declared |
| AI Interaction | `OLLAMA_MODEL`, `OLLAMA_DISABLE_THINKING` | `true` | chat replies are plain text on every model; no JSON params are ever sent |

Booleans are parsed strictly (`true/false/1/0/yes/no/on/off`); a typo raises at startup instead of
silently picking a default.

How it behaves:

* `AIRequest.wants_json` (hub-api) / `generate(..., want_json=True)` (Researcher) is a **request**.
  Ollama's `format: json` is sent **only** when the configured model `supports_json`; otherwise the
  call is plain text.
* The reply says which one happened -- `AIResponse.json_mode` -- and in JSON mode the text has
  already been validated as JSON (a non-JSON reply is a typed error, not a text blob in disguise).
  Callers must check `json_mode`; the router's fallback ladder can land on a text-only tier even
  when the requested tier is JSON-capable.
* `think:false` is sent by default. Reasoning models such as gemma4 otherwise spend the whole
  `num_predict` budget on hidden thinking and return an **empty** answer; every client now fails
  loudly (`provider_error` / `EmptyCompletionError` / logged `None`) on a blank completion instead
  of returning it as a successful, metered reply.

Example (free = text-only e2b, premium = JSON-capable e4b):

```bash
OLLAMA_URL=http://ollama:11434
AI_FREE_MODEL=gemma4:e2b                 # AI_FREE_SUPPORTS_JSON left unset -> text path
AI_PREMIUM_MODEL=gemma4:e4b
AI_PREMIUM_SUPPORTS_JSON=true
```

## Observability

Every model call emits PII-free OTel (`flask_core.ai_telemetry`): span `ai.provider.generate`,
histogram `waddles.ai.provider.duration`, counters `waddles.ai.provider.tokens` /
`waddles.ai.provider.errors`. Destination is the standard OTLP env vars; with none configured the
calls are no-ops. Offline tests assert spans and metric points are actually received (counts are
printed; zero is a failure).

## Known gaps found while writing these tests (not fixed here)

* **No LLM-based safety classifier is wired anywhere.** `shieldgemma` is exercised only as "the
  safety model works on the standard text path"; the Researcher's gate is the regex/topic
  `SafetyLayer`. A shieldgemma verdict consumer is new product work.
* `ResearchService` still calls `rate_limiter.check_limit(key, limit_type)` and
  `mem0_service.search(query=, community_id=, ...)` / `.add(...)`, but the real `RateLimiter.check_limit`
  takes `(community_id, user_id, limit_type)` and returns a `RateLimitResult`, and `Mem0Service` has
  `search(query, limit)` / `add_memory(...)`. Those errors are swallowed (rate limiting fails open;
  semantic cache silently misses), so the rate limit is currently not enforced. Tests use stand-ins
  for those stores; fixing the contracts is a follow-up.
* `AI_PROVIDER=waddleai` (a documented option) makes `AIProviderService` raise at construction --
  there is no WaddleAI branch in the Researcher's `AIProvider` enum.
* `SummaryService` talks to the DB via `db.execute(query, [params])` (asyncpg-style) on an `AsyncDAL`
  handle; the live path is untested.
* `core/ai_researcher_module/tests/test_security_headers_cors.py::...::test_healthz_is_exempt_from_the_auth_gate`
  is host-CPU-sensitive (`/healthz` returns 503 when `psutil.cpu_percent()` > 95) and flakes on a
  loaded machine; unrelated to these changes.
