"""
Provider abstraction for medical symptom classification.

Providers are tried in order: HuggingFace → OpenAI → Gemini → hardcoded fallback.
Each provider implements BaseProvider.classify() and declares is_available so the
fallback chain skips providers whose credentials are not configured.

Circuit breakers (aiobreaker) are attached per-provider: after 3 consecutive
failures the breaker opens for 60 seconds, causing the chain to skip that
provider immediately instead of waiting for retries to exhaust.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod

import structlog
from aiobreaker import CircuitBreaker, CircuitBreakerError
from tenacity import (
    before_sleep_log,
    retry,
    stop_after_attempt,
    wait_exponential,
)

from app.core.constants import CONDITION_LABELS

logger = structlog.get_logger(__name__)
_tenacity_logger = logging.getLogger(__name__)

# ── Circuit breakers (one per provider) ─────────────────────────────────────
# After fail_max consecutive failures the breaker opens and raises
# CircuitBreakerError immediately, skipping retries for timeout_duration seconds.
_hf_breaker = CircuitBreaker(fail_max=3, timeout_duration=60)
_oai_breaker = CircuitBreaker(fail_max=3, timeout_duration=60)
_gem_breaker = CircuitBreaker(fail_max=3, timeout_duration=60)


# ── Base class ───────────────────────────────────────────────────────────────

class BaseProvider(ABC):
    name: str

    @abstractmethod
    async def classify(self, symptoms: str) -> dict:
        """Classify symptoms and return a result dict.

        Returns:
            {
                "top_condition": str,
                "confidence": float,
                "all_predictions": list[{"label": str, "score": float}],
                "provider": str,
            }
        Raises any exception on failure — the fallback chain handles it.
        """
        ...

    @property
    def is_available(self) -> bool:
        """Return True when this provider can be used (credentials present, etc.)."""
        return True


# ── HuggingFace provider ─────────────────────────────────────────────────────

class HuggingFaceProvider(BaseProvider):
    name = "huggingface"

    def __init__(self) -> None:
        self._clf = None

    def _load(self):
        """Synchronous model loader — safe to call from run_in_executor for warm-up."""
        if self._clf is None:
            from transformers import pipeline

            model = os.getenv("MODEL_NAME", "facebook/bart-large-mnli")
            logger.info("hf_model_loading", model=model)
            self._clf = pipeline("zero-shot-classification", model=model)
            logger.info("hf_model_loaded", model=model)
        return self._clf

    @_hf_breaker
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
        before_sleep=before_sleep_log(_tenacity_logger, logging.WARNING),
    )
    async def classify(self, symptoms: str) -> dict:
        loop = asyncio.get_running_loop()
        clf = await loop.run_in_executor(None, self._load)
        result = await loop.run_in_executor(
            None,
            lambda: clf(symptoms, CONDITION_LABELS, multi_label=False),
        )
        all_preds = [
            {"label": lbl, "score": round(score, 4)}
            for lbl, score in zip(result["labels"], result["scores"])
        ]
        return {
            "top_condition": all_preds[0]["label"],
            "confidence": all_preds[0]["score"],
            "all_predictions": all_preds,
            "provider": self.name,
        }


# ── OpenAI provider ──────────────────────────────────────────────────────────

class OpenAIProvider(BaseProvider):
    name = "openai"

    @property
    def is_available(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    @_oai_breaker
    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    async def classify(self, symptoms: str) -> dict:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        labels_str = ", ".join(CONDITION_LABELS)
        prompt = (
            f"You are a medical AI. Given symptoms: '{symptoms}'\n"
            f"Pick the single most likely condition from: {labels_str}.\n"
            'Respond with JSON: {"condition": "<label>", "confidence": <0.0-1.0>}'
        )
        response = await client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content)
        condition = data.get("condition", CONDITION_LABELS[0])
        confidence = float(data.get("confidence", 0.5))
        return {
            "top_condition": condition,
            "confidence": confidence,
            "all_predictions": [{"label": condition, "score": confidence}],
            "provider": self.name,
        }


# ── Gemini provider ──────────────────────────────────────────────────────────

class GeminiProvider(BaseProvider):
    name = "gemini"

    @property
    def is_available(self) -> bool:
        return bool(os.getenv("GEMINI_API_KEY"))

    @_gem_breaker
    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    async def classify(self, symptoms: str) -> dict:
        import httpx

        api_key = os.getenv("GEMINI_API_KEY")
        labels_str = ", ".join(CONDITION_LABELS)
        prompt = (
            f"Given symptoms: '{symptoms}'\n"
            f"Pick the single most likely condition from: {labels_str}.\n"
            'Respond ONLY with JSON: {"condition": "<label>", "confidence": <0.0-1.0>}'
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={api_key}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
            )
            resp.raise_for_status()
        data = json.loads(
            resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        )
        condition = data.get("condition", CONDITION_LABELS[0])
        confidence = float(data.get("confidence", 0.5))
        return {
            "top_condition": condition,
            "confidence": confidence,
            "all_predictions": [{"label": condition, "score": confidence}],
            "provider": self.name,
        }


# ── Module-level singletons ──────────────────────────────────────────────────

_hf_provider = HuggingFaceProvider()

_PROVIDERS: list[BaseProvider] = [
    _hf_provider,
    OpenAIProvider(),
    GeminiProvider(),
]


# ── Public API ───────────────────────────────────────────────────────────────

async def classify_with_fallback(symptoms: str) -> tuple[dict, bool]:
    """Try providers in order; return (result, is_fallback).

    is_fallback=True only when every available provider fails or has its
    circuit breaker open.
    """
    for provider in _PROVIDERS:
        if not provider.is_available:
            continue
        try:
            result = await provider.classify(symptoms)
            logger.info("provider_success", provider=provider.name)
            return result, False
        except CircuitBreakerError:
            logger.warning("provider_circuit_open", provider=provider.name)
        except Exception as exc:
            logger.warning(
                "provider_failed", provider=provider.name, error=str(exc)
            )

    logger.error("all_providers_failed")
    return {
        "top_condition": "unclassifiable",
        "confidence": 0.0,
        "all_predictions": [],
        "provider": "none",
    }, True
