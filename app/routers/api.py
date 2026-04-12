import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.limiter import limiter
from app.core.security import require_api_key
from app.models.schemas import HealthResponse, PredictionResponse, SymptomRequest
from app.services import core

logger = structlog.get_logger(__name__)

router = APIRouter()


# ──────────────────────────────────────────────
# Endpoint 1 — POST /predict
# ──────────────────────────────────────────────
@router.post(
    "/predict",
    response_model=PredictionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Classify patient symptoms",
    description=(
        "Accepts a free-text symptom description, runs classification via the "
        "multi-provider fallback chain (HuggingFace → OpenAI → Gemini), stores "
        "the result in PostgreSQL, and returns the prediction with confidence scores."
    ),
    tags=["Prediction"],
    dependencies=[Depends(require_api_key)],
)
@limiter.limit("30/minute")
async def predict(request: Request, payload: SymptomRequest):
    """
    **Required fields:** `patient_id`, `symptoms`

    Raises **422** if `symptoms` is blank, too short/long, or `age` is out of range.
    Returns **201** even when the model is unavailable; check ``is_fallback`` in the
    response body and advise the patient to consult a doctor when it is ``true``.

    Requires `X-API-Key` header (or Bearer JWT via Authorization header).
    Rate-limited to 30 requests per minute per IP.
    """
    try:
        result = await core.classify_symptoms(
            patient_id=payload.patient_id,
            symptoms=payload.symptoms,
            age=payload.age,
            notes=payload.notes,
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
        logger.error("db_error_on_predict", patient_id=payload.patient_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable; the prediction could not be saved. Please retry later.",
        ) from exc
    return result


# ──────────────────────────────────────────────
# Endpoint 2 — GET /predict/{record_id}
# ──────────────────────────────────────────────
@router.get(
    "/predict/{record_id}",
    response_model=PredictionResponse,
    summary="Retrieve a saved prediction",
    description="Fetch a previously stored prediction by its database record ID.",
    tags=["Prediction"],
    dependencies=[Depends(require_api_key)],
)
async def get_prediction(record_id: int):
    """Returns **404** if the record does not exist.

    Requires `X-API-Key` header (or Bearer JWT via Authorization header).
    """
    result = await core.get_prediction_by_id(record_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Prediction record {record_id} not found")
    return result


# ──────────────────────────────────────────────
# Endpoint 3 — GET /health  (public)
# ──────────────────────────────────────────────
@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description="Returns the live status of the API and database connection. Public — no auth required.",
    tags=["Ops"],
)
async def health():
    db_ok = await core.check_db_health()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "healthy" if db_ok else "unreachable",
        "model": "multi-provider",
        "version": core.MODEL_VERSION,
    }
