"""
Business logic: orchestrates multi-provider classification and DB persistence.

Inference is delegated to app.services.providers.classify_with_fallback which
tries HuggingFace → OpenAI → Gemini in order.  If all providers fail,
is_fallback=True is returned and the record is still persisted for audit.

Database errors (asyncpg.PostgresError) propagate to the caller unchanged;
the caller maps them to an HTTP response.
"""

import json
import os
import time
from typing import Optional

import asyncpg
import structlog
from opentelemetry import metrics as otel_metrics

from app.services.providers import classify_with_fallback

logger = structlog.get_logger(__name__)

MODEL_VERSION = os.getenv("MODEL_VERSION", "1.0.0")
MODEL_NAME = os.getenv("MODEL_NAME", "facebook/bart-large-mnli")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    pg_user = os.getenv("POSTGRES_USER", "postgres")
    pg_password = os.getenv("POSTGRES_PASSWORD", "")
    pg_host = os.getenv("POSTGRES_HOST", "localhost")
    pg_port = os.getenv("POSTGRES_PORT", "5432")
    pg_db = os.getenv("POSTGRES_DB", "medicaldb")
    if not pg_password:
        raise RuntimeError(
            "Either DATABASE_URL or POSTGRES_PASSWORD environment variable must be set."
        )
    DATABASE_URL = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"

_db_pool: Optional[asyncpg.Pool] = None

FALLBACK_MESSAGE = "Không thể phân loại, vui lòng tham khảo bác sĩ"

# ── OTel custom metrics ──────────────────────────────────────────────────────
# Instruments are created at module level after setup_telemetry() has run
# (setup_telemetry is called at module level in main.py, before any import of
# this module triggers these create_* calls).
_meter = otel_metrics.get_meter("medical-ai-service")
_predictions_counter = _meter.create_counter(
    "medical_ai_predictions_total",
    description="Total classification requests, labelled by provider.",
)
_fallback_counter = _meter.create_counter(
    "medical_ai_fallback_total",
    description="Total responses where all providers failed (is_fallback=True).",
)
_inference_histogram = _meter.create_histogram(
    "medical_ai_inference_duration_ms",
    description="End-to-end inference latency across the provider fallback chain, in ms.",
    unit="ms",
)

# ── Langfuse client (optional) ───────────────────────────────────────────────
_langfuse = None
if os.getenv("LANGFUSE_PUBLIC_KEY"):
    try:
        from langfuse import Langfuse
        _langfuse = Langfuse()
    except Exception as _lf_err:
        logger.warning("langfuse_init_failed", error=str(_lf_err))


async def get_db_pool() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _db_pool


async def init_db():
    """Create predictions table if not exists and apply migration guards."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id            SERIAL PRIMARY KEY,
                patient_id    VARCHAR(64) NOT NULL,
                symptoms      TEXT NOT NULL,
                top_condition VARCHAR(128),
                confidence    FLOAT,
                all_predictions JSONB,
                model_version VARCHAR(32),
                age           SMALLINT,
                notes         TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            ALTER TABLE predictions
                ADD COLUMN IF NOT EXISTS age         SMALLINT,
                ADD COLUMN IF NOT EXISTS notes       TEXT,
                ADD COLUMN IF NOT EXISTS is_fallback BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS provider    VARCHAR(32) DEFAULT 'huggingface'
        """)
    logger.info("db_initialized")


async def classify_symptoms(
    patient_id: str,
    symptoms: str,
    age: int | None,
    notes: str | None,
) -> dict:
    """Run classification via provider fallback chain and persist result.

    Model errors are suppressed: if all providers fail, is_fallback=True is
    returned and a human-readable fallback_message is included.  The record is
    still persisted to the database for audit purposes.

    Database errors (asyncpg.PostgresError) propagate to the caller unchanged.
    """

    # ── Langfuse trace (outer wrapper, provider-agnostic) ────────────────────
    lf_trace = None
    lf_generation = None
    if _langfuse is not None:
        lf_trace = _langfuse.trace(
            name="classify_symptoms",
            user_id=patient_id,
            metadata={"age": age, "notes": notes},
        )
        lf_generation = lf_trace.generation(
            name="classify_with_fallback",
            input=symptoms,
        )

    # ── Inference via provider fallback chain ────────────────────────────────
    t0 = time.perf_counter()
    result_data, is_fallback = await classify_with_fallback(symptoms)
    duration_ms = (time.perf_counter() - t0) * 1000

    provider = result_data.get("provider", "none")
    top_condition = result_data["top_condition"]
    confidence = result_data["confidence"]
    all_preds = result_data["all_predictions"]

    # ── OTel metrics ─────────────────────────────────────────────────────────
    _predictions_counter.add(1, {"provider": provider})
    if is_fallback:
        _fallback_counter.add(1)
    _inference_histogram.record(duration_ms, {"provider": provider})

    # ── Langfuse generation end ──────────────────────────────────────────────
    if lf_generation is not None:
        if is_fallback:
            lf_generation.end(level="ERROR", status_message="all_providers_failed")
        else:
            lf_generation.end(
                output=top_condition,
                metadata={"provider": provider, "confidence": round(confidence, 4)},
            )

    # ── Persist to DB ────────────────────────────────────────────────────────
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO predictions
                (patient_id, symptoms, top_condition, confidence, all_predictions,
                 model_version, age, notes, is_fallback, provider)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10)
            RETURNING id, created_at
            """,
            patient_id,
            symptoms,
            top_condition,
            confidence,
            json.dumps(all_preds),
            MODEL_VERSION,
            age,
            notes,
            is_fallback,
            provider,
        )

    logger.info(
        "prediction_saved",
        patient_id=patient_id,
        record_id=row["id"],
        top_condition=top_condition,
        confidence=round(confidence, 4),
        provider=provider,
        is_fallback=is_fallback,
        duration_ms=round(duration_ms, 1),
    )

    response: dict = {
        "record_id": row["id"],
        "patient_id": patient_id,
        "symptoms": symptoms,
        "top_condition": top_condition,
        "confidence": confidence,
        "all_predictions": all_preds,
        "model_version": MODEL_VERSION,
        "created_at": row["created_at"],
        "is_fallback": is_fallback,
    }
    if is_fallback:
        response["fallback_message"] = FALLBACK_MESSAGE
    return response


async def get_prediction_by_id(record_id: int) -> dict | None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM predictions WHERE id = $1", record_id)
    if row is None:
        return None
    is_fallback = bool(row["is_fallback"]) if row["is_fallback"] is not None else False
    all_predictions = row["all_predictions"]
    if isinstance(all_predictions, str):
        all_predictions = json.loads(all_predictions)
    elif all_predictions is None:
        all_predictions = []
    record: dict = {
        "record_id": row["id"],
        "patient_id": row["patient_id"],
        "symptoms": row["symptoms"],
        "top_condition": row["top_condition"],
        "confidence": row["confidence"],
        "all_predictions": all_predictions,
        "model_version": row["model_version"],
        "created_at": row["created_at"],
        "is_fallback": is_fallback,
    }
    if is_fallback:
        record["fallback_message"] = FALLBACK_MESSAGE
    return record


async def close_db_pool():
    global _db_pool
    if _db_pool is not None:
        await _db_pool.close()
        _db_pool = None
        logger.info("db_pool_closed")


async def check_db_health() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
