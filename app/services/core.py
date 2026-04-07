"""
Business logic: wraps the HuggingFace zero-shot classification pipeline
and handles DB persistence via asyncpg.
"""
import json
import os
import asyncio
import asyncpg
import logging
from datetime import datetime, timezone
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("MODEL_NAME", "facebook/bart-large-mnli")
MODEL_VERSION = os.getenv("MODEL_VERSION", "1.0.0")

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

# Medical condition labels for zero-shot classification
CONDITION_LABELS = [
    "migraine",
    "bacterial meningitis",
    "common cold",
    "influenza",
    "COVID-19",
    "hypertension",
    "appendicitis",
    "urinary tract infection",
    "anxiety disorder",
    "pneumonia",
]

_classifier = None
_db_pool: Optional[asyncpg.Pool] = None

FALLBACK_MESSAGE = "Không thể phân loại, vui lòng tham khảo bác sĩ"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _run_inference(clf, symptoms: str) -> dict:
    """Synchronous inference call; tenacity retries up to 3 times with exponential backoff."""
    return clf(symptoms, CONDITION_LABELS, multi_label=False)


def get_classifier():
    """Lazy-load classifier; cached after first call."""
    global _classifier
    if _classifier is None:
        try:
            from transformers import pipeline
            logger.info(f"Loading model: {MODEL_NAME}")
            _classifier = pipeline("zero-shot-classification", model=MODEL_NAME)
            logger.info("Model loaded successfully")
        except Exception as e:
            logger.error(f"Model load failed: {e}")
            _classifier = None
    return _classifier


async def get_db_pool() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _db_pool


async def init_db():
    """Create predictions table if not exists."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id          SERIAL PRIMARY KEY,
                patient_id  VARCHAR(64) NOT NULL,
                symptoms    TEXT NOT NULL,
                top_condition VARCHAR(128),
                confidence  FLOAT,
                all_predictions JSONB,
                model_version VARCHAR(32),
                age         SMALLINT,
                notes       TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migration guard: add columns to pre-existing tables that lack them.
        await conn.execute("""
            ALTER TABLE predictions
                ADD COLUMN IF NOT EXISTS age         SMALLINT,
                ADD COLUMN IF NOT EXISTS notes       TEXT,
                ADD COLUMN IF NOT EXISTS is_fallback BOOLEAN DEFAULT FALSE
        """)
    logger.info("DB initialized")


async def classify_symptoms(patient_id: str, symptoms: str, age: int | None, notes: str | None) -> dict:
    """Run zero-shot classification and persist result.

    Never raises — if the model is unavailable or all retries fail the response
    includes ``is_fallback=True`` and a human-readable ``fallback_message``.
    The fallback record is still persisted to the database for audit purposes.
    """
    clf = get_classifier()
    is_fallback = False

    if clf is None:
        logger.warning("Classifier unavailable — returning fallback response")
        is_fallback = True
    else:
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: _run_inference(clf, symptoms))
        except Exception as exc:
            logger.error("Inference failed after all retries: %s", exc)
            is_fallback = True

    if is_fallback:
        top_condition = "unclassifiable"
        confidence = 0.0
        all_preds: list[dict] = []
    else:
        all_preds = [
            {"label": lbl, "score": round(score, 4)}
            for lbl, score in zip(result["labels"], result["scores"])
        ]
        top_condition = all_preds[0]["label"]
        confidence = all_preds[0]["score"]

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO predictions
                (patient_id, symptoms, top_condition, confidence, all_predictions,
                 model_version, age, notes, is_fallback)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
            RETURNING id, created_at
            """,
            patient_id, symptoms, top_condition, confidence,
            json.dumps(all_preds), MODEL_VERSION, age, notes, is_fallback,
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
    record: dict = {
        "record_id": row["id"],
        "patient_id": row["patient_id"],
        "symptoms": row["symptoms"],
        "top_condition": row["top_condition"],
        "confidence": row["confidence"],
        "all_predictions": json.loads(row["all_predictions"]),
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
        logger.info("DB pool closed")


async def check_db_health() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
