from fastapi import APIRouter, HTTPException, status
from app.models.schemas import SymptomRequest, PredictionResponse, HealthResponse
from app.services import core

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
        "Accepts a free-text symptom description, runs zero-shot classification "
        "against a set of medical conditions, stores the result in PostgreSQL, "
        "and returns the prediction with confidence scores."
    ),
    tags=["Prediction"],
)
async def predict(payload: SymptomRequest):
    """
    **Required fields:** `patient_id`, `symptoms`

    Raises **422** if `symptoms` is blank, too short/long, or `age` is out of range.
    Raises **503** if the model failed to load.
    """
    try:
        result = await core.classify_symptoms(
            patient_id=payload.patient_id,
            symptoms=payload.symptoms,
            age=payload.age,
            notes=payload.notes,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
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
)
async def get_prediction(record_id: int):
    """Returns **404** if the record does not exist."""
    result = await core.get_prediction_by_id(record_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Prediction record {record_id} not found")
    return result


# ──────────────────────────────────────────────
# Endpoint 3 — GET /health
# ──────────────────────────────────────────────
@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description="Returns the live status of the API, database connection, and model loader.",
    tags=["Ops"],
)
async def health():
    db_ok = await core.check_db_health()
    clf_ok = core.get_classifier() is not None
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "healthy" if db_ok else "unreachable",
        "model": "loaded" if clf_ok else "unavailable",
        "version": core.MODEL_VERSION,
    }
