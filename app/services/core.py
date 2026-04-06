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
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    logger.info("DB initialized")


async def classify_symptoms(patient_id: str, symptoms: str, age: int | None, notes: str | None) -> dict:
    """Run zero-shot classification and persist result."""
    clf = get_classifier()
    if clf is None:
        raise RuntimeError(f"Model '{MODEL_NAME}' is unavailable. Check logs for load errors.")

    result = clf(symptoms, CONDITION_LABELS, multi_label=False)
    all_preds = [{"label": l, "score": round(s, 4)} for l, s in zip(result["labels"], result["scores"])]
    top_condition = all_preds[0]["label"]
    confidence = all_preds[0]["score"]
    model_ver = MODEL_VERSION

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO predictions (patient_id, symptoms, top_condition, confidence, all_predictions, model_version)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING id, created_at
            """,
            patient_id, symptoms, top_condition, confidence, json.dumps(all_preds), model_ver
        )

    return {
        "record_id": row["id"],
        "patient_id": patient_id,
        "symptoms": symptoms,
        "top_condition": top_condition,
        "confidence": confidence,
        "all_predictions": all_preds,
        "model_version": model_ver,
        "created_at": row["created_at"],
    }


async def get_prediction_by_id(record_id: int) -> dict | None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM predictions WHERE id = $1", record_id)
    if row is None:
        return None
    return {
        "record_id": row["id"],
        "patient_id": row["patient_id"],
        "symptoms": row["symptoms"],
        "top_condition": row["top_condition"],
        "confidence": row["confidence"],
        "all_predictions": json.loads(row["all_predictions"]),
        "model_version": row["model_version"],
        "created_at": row["created_at"],
    }


async def check_db_health() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
