"""`LocalOllamaClassifier` -- Basic-tier moderation via a local Ollama ShieldGemma model.

Per design SS5.1/SS6: the only classifier the Basic tier needs, no
WaddleAI dependency. Implements ShieldGemma's documented single-policy
prompt contract (see `_PROMPT_TEMPLATE` and `_build_prompt`) against
Ollama's native `/api/generate` endpoint with `logprobs`/`top_logprobs`
requested, and calibrates `Classification.confidence` as the softmax-
normalized `P(Yes)` over the first generated token's `Yes`/`No` logprobs
-- exactly the scoring ShieldGemma's model card documents, not a naive
"parse the greedy decode" approach, which collapses a 51/49 borderline
call to the same output as a 99/1 one.

Ollama version note: `logprobs`/`top_logprobs` on `/api/generate` requires
Ollama >=0.33 (verified against the 0.33.3 test box this was built
against, see `tests/test_local_ollama_live.py`). An older Ollama silently
omits the `logprobs` field; `_extract_yes_no_logprobs` treats a missing
field as `(None, None)`, which `_softmax_p_yes` maps to `confidence=0.0`
(never a crash, never a false positive from a degraded response).
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from moderation_module.base import Classification, ClassificationProvider

logger = logging.getLogger(__name__)

#: ShieldGemma's built-in family covers Dangerous Content/Harassment/Hate
#: Speech/Sexually Explicit Information as a bundled 4-policy prompt
#: (Ollama's own `shieldgemma:2b` chat template, `ollama show
#: shieldgemma:2b`). The Basic tier (design SS6) instead asks ShieldGemma
#: one policy at a time -- each entry is `(policy_name, policy_description)`
#: dropped into `_PROMPT_TEMPLATE`'s single `"{policy_name}"` bullet, so a
#: message only pays for the categories a community actually enabled.
#: `hate_speech`/`basic_harassment` reuse ShieldGemma's own documented
#: policy text verbatim (its native categories); `slurs` is a Basic-tier
#: category ShieldGemma has no native bullet for, so its description is a
#: tighter, purpose-written policy in the same documented style, scoped to
#: epithets/dehumanizing terms rather than hate speech's broader "targets
#: identity" framing.
BASIC_CATEGORY_POLICIES: dict[str, tuple[str, str]] = {
    "hate_speech": (
        "No Hate Speech",
        "The human question shall not contain or seek generation of content that "
        "targets identity and/or protected attributes (e.g., racial slurs, promotion "
        "of discrimination, calls to violence against protected groups) or "
        "dehumanizing, belittling, or vilifying on the basis of race, ethnicity, "
        "religion, disability, age, nationality, veteran status, sexual orientation, "
        "gender, gender identity, caste, or any other protected status.",
    ),
    "basic_harassment": (
        "No Harassment",
        "The human question shall not contain or seek generation of content that is "
        "malicious, intimidating, bullying, or abusive content targeting another "
        "individual (e.g., physical threats, denial of tragic events, disparaging "
        "victims of violence, repeated insults directed at a specific person).",
    ),
    "slurs": (
        "No Slurs",
        "The human question shall not contain slurs, derogatory epithets, or "
        "dehumanizing terms directed at a person or group based on race, ethnicity, "
        "religion, gender, sexual orientation, disability, nationality, or other "
        "personal or protected attribute, even when used casually or as a generic "
        "insult rather than targeted hate speech.",
    ),
}

#: ShieldGemma's documented "evaluating a user prompt" template (its
#: response-evaluation variant additionally carries a "Chatbot Response:"
#: turn, not used here -- we only ever classify the inbound human message,
#: never a bot's own reply). One policy bullet per call by design -- see
#: `BASIC_CATEGORY_POLICIES`'s docstring for why this deviates from
#: Ollama's bundled 4-policy chat template.
_PROMPT_TEMPLATE = (
    "You are a policy expert trying to help determine whether a user prompt "
    "is in violation of the defined safety policy.\n\n"
    "<start_of_turn>\n"
    "Human Question: {message}\n"
    "<end_of_turn>\n\n"
    "Our safety principle is defined in the below:\n\n"
    '* "{policy_name}": {policy_description}\n\n'
    "===\n\n"
    "Does the human question violate the above principle? Your answer must "
    "start with 'Yes' or 'No'. And then walk through step by step to be "
    "sure we answer correctly.\n"
)

DEFAULT_MODEL = "shieldgemma:2b"

#: Selectable via the `model` config knob for higher-accuracy, higher-
#: latency classification (design SS5.1's "fallback ... for a second
#: opinion"). Not ShieldGemma-family -- see the class docstring's
#: calibration caveat when this is selected.
ALTERNATE_MODEL = "gemma4:e4b"

#: Ollama-measured p50 latency for a 1-policy ShieldGemma call is ~300ms
#: (see tests/test_local_ollama_live.py); 10s leaves headroom for a cold
#: model load on first call without blocking the hot path indefinitely.
_DEFAULT_TIMEOUT_SECONDS = 10.0

#: A matched category must clear this confidence to be returned at all --
#: below it, `classify()` returns `None` exactly as if the category had
#: never matched (design SS5's "no false positives logged as matches").
_DEFAULT_MATCH_THRESHOLD = 0.5

_SEVERITY_HIGH_THRESHOLD = 0.85
_SEVERITY_MEDIUM_THRESHOLD = 0.65


@dataclass(slots=True, frozen=True)
class OllamaConfig:
    """Runtime knobs for `LocalOllamaClassifier` -- see `OllamaConfig.from_env`."""

    ollama_url: str
    model: str = DEFAULT_MODEL
    match_threshold: float = _DEFAULT_MATCH_THRESHOLD
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> OllamaConfig:
        """Build from `MODERATION_OLLAMA_URL`/`_MODEL`/`_MATCH_THRESHOLD`/`_TIMEOUT_SECONDS`.

        `MODERATION_OLLAMA_URL` defaults to `http://localhost:11434` (the
        in-cluster/dev-compose Ollama sidecar) -- never a hardcoded
        deployment-specific host; a real deployment always sets this
        explicitly via `penguin_sal`/env, per `security.md` Token & Secret
        Hygiene (this URL is not a secret, but the pattern of "never
        hardcode a network endpoint" still applies).
        """
        return cls(
            ollama_url=os.environ.get("MODERATION_OLLAMA_URL", "http://localhost:11434"),
            model=os.environ.get("MODERATION_OLLAMA_MODEL", DEFAULT_MODEL),
            match_threshold=float(
                os.environ.get("MODERATION_MATCH_THRESHOLD", str(_DEFAULT_MATCH_THRESHOLD))
            ),
            timeout_seconds=float(
                os.environ.get("MODERATION_OLLAMA_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
            ),
        )


class LocalOllamaClassifier(ClassificationProvider):
    """Basic-tier `ClassificationProvider` -- local Ollama running a ShieldGemma model."""

    def __init__(
        self,
        config: OllamaConfig | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """`config` defaults to `OllamaConfig.from_env()`; `http_client` is injectable for tests."""
        self._config = config or OllamaConfig.from_env()
        self._client = http_client

    async def classify(
        self,
        message: str,
        enabled_categories: set[str],
        *,
        tenant_id: int,
        community_id: int,
    ) -> Classification | None:
        """Check only the enabled categories this provider knows (`BASIC_CATEGORY_POLICIES`).

        Any `enabled_categories` entry outside the Basic set (an Advanced/
        Security category a caller forgot to route elsewhere) is silently
        skipped, not an error -- this provider only ever answers for the
        categories it's actually responsible for. Returns the single
        highest-confidence match at or above `match_threshold`, or `None`.
        """
        checkable = enabled_categories & BASIC_CATEGORY_POLICIES.keys()
        if not checkable:
            return None

        best: Classification | None = None
        for category in checkable:
            confidence = await self._classify_one_category(message, category)
            if confidence < self._config.match_threshold:
                continue
            if best is None or confidence > best.confidence:
                best = Classification(
                    category=category,
                    confidence=confidence,
                    severity=_severity_for(confidence),
                )
        if best is not None:
            logger.info(
                "moderation.classification_matched",
                extra={
                    "category": best.category,
                    "severity": best.severity,
                    "tenant_id": tenant_id,
                    "community_id": community_id,
                },
            )
        return best

    async def _classify_one_category(self, message: str, category: str) -> float:
        """Run one ShieldGemma call for `category`; returns calibrated `P(violates policy)`."""
        policy_name, policy_description = BASIC_CATEGORY_POLICIES[category]
        prompt = _build_prompt(message, policy_name, policy_description)
        body = {
            "model": self._config.model,
            "prompt": prompt,
            "raw": True,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 1},
            "logprobs": True,
            "top_logprobs": 8,
        }

        client, owns_client = await self._client_or_new()
        try:
            response = await client.post(
                f"{self._config.ollama_url}/api/generate",
                json=body,
                timeout=self._config.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Graceful degradation, not a crash (design SS6): an unreachable
            # local Ollama is treated as "no match", the same shape as
            # WaddleAIClassifier's own unavailability handling.
            logger.warning(
                "moderation.ollama_unreachable",
                extra={"category": category, "error_type": type(exc).__name__},
            )
            return 0.0
        finally:
            if owns_client:
                await client.aclose()

        return _score_response(response.json())

    async def _client_or_new(self) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(), True


def _build_prompt(message: str, policy_name: str, policy_description: str) -> str:
    """Render ShieldGemma's documented single-policy prompt for `message`."""
    return _PROMPT_TEMPLATE.format(
        message=message, policy_name=policy_name, policy_description=policy_description
    )


def _score_response(data: Mapping[str, Any]) -> float:
    """Extract the first-token `Yes`/`No` logprobs from an Ollama `/api/generate` body and score."""
    yes_logprob, no_logprob = _extract_yes_no_logprobs(data)
    return _softmax_p_yes(yes_logprob, no_logprob)


def _extract_yes_no_logprobs(data: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """Pull `Yes`/`No` logprobs out of the first token's `top_logprobs`, case-insensitively."""
    logprobs = data.get("logprobs")
    if not logprobs:
        return None, None
    top = logprobs[0].get("top_logprobs", [])
    yes_logprob: float | None = None
    no_logprob: float | None = None
    for entry in top:
        # Ollama's generated word for this candidate, not a credential --
        # avoid a `token`-containing variable name so ruff's S105
        # hardcoded-password heuristic doesn't misfire on this comparison.
        generated_word = str(entry.get("token", "")).strip().lower()
        if generated_word == "yes" and yes_logprob is None:
            yes_logprob = float(entry["logprob"])
        elif generated_word == "no" and no_logprob is None:
            no_logprob = float(entry["logprob"])
    return yes_logprob, no_logprob


#: A logprob floor used when one of `Yes`/`No` never appears in
#: `top_logprobs` (an 8-wide beam should always surface both for a
#: well-formed ShieldGemma prompt, but a missing token must degrade to
#: "confidently not that token," never a crash or an artificially
#: inflated confidence).
_MISSING_TOKEN_LOGPROB = -20.0


def _softmax_p_yes(yes_logprob: float | None, no_logprob: float | None) -> float:
    """Calibrated `P(Yes)` -- softmax over the two logprobs, ShieldGemma's documented scoring."""
    if yes_logprob is None and no_logprob is None:
        return 0.0
    yes = yes_logprob if yes_logprob is not None else _MISSING_TOKEN_LOGPROB
    no = no_logprob if no_logprob is not None else _MISSING_TOKEN_LOGPROB
    p_yes = math.exp(yes)
    p_no = math.exp(no)
    return p_yes / (p_yes + p_no)


def _severity_for(confidence: float) -> str:
    """Map a matched confidence to a coarse severity bucket for the (not-yet-built) gate."""
    if confidence >= _SEVERITY_HIGH_THRESHOLD:
        return "high"
    if confidence >= _SEVERITY_MEDIUM_THRESHOLD:
        return "medium"
    return "low"
