"""Smoke tests for the /health and /predict endpoints."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

MOCK_PREDICTION = {
    "record_id": 1,
    "patient_id": "P-001",
    "symptoms": "Patient reports persistent headache, fever of 38.5°C, and stiff neck for 3 days",
    "top_condition": "bacterial meningitis",
    "confidence": 0.72,
    "all_predictions": [
        {"label": "bacterial meningitis", "score": 0.72},
        {"label": "migraine", "score": 0.15},
    ],
    "model_version": "1.0.0-mock",
    "created_at": datetime.now(timezone.utc),
}


@pytest.fixture(scope="session")
def client():
    """
    Build a TestClient that suppresses real DB and model I/O during the
    lifespan startup (init_db + get_classifier warm-up).
    """
    with (
        patch("app.services.core.init_db", new_callable=AsyncMock),
        patch("app.services.core.get_classifier", return_value=None),
    ):
        from app.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_200(client: TestClient):
    with (
        patch("app.services.core.check_db_health", new_callable=AsyncMock, return_value=True),
        patch("app.services.core.get_classifier", return_value=MagicMock()),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "healthy"
    assert body["model"] == "loaded"
    assert "version" in body


def test_health_degraded_when_db_unreachable(client: TestClient):
    with patch("app.services.core.check_db_health", new_callable=AsyncMock, return_value=False):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unreachable"


# ---------------------------------------------------------------------------
# POST /predict — happy path
# ---------------------------------------------------------------------------


def test_predict_returns_201(client: TestClient):
    with patch("app.services.core.classify_symptoms", new_callable=AsyncMock, return_value=MOCK_PREDICTION):
        response = client.post(
            "/predict",
            json={
                "patient_id": "P-001",
                "symptoms": "Patient reports persistent headache, fever of 38.5°C, and stiff neck for 3 days",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["record_id"] == 1
    assert body["top_condition"] == "bacterial meningitis"
    assert 0.0 <= body["confidence"] <= 1.0
    assert isinstance(body["all_predictions"], list)
    assert all("label" in p and "score" in p for p in body["all_predictions"])


def test_predict_passes_optional_fields(client: TestClient):
    with patch("app.services.core.classify_symptoms", new_callable=AsyncMock, return_value=MOCK_PREDICTION) as mock_clf:
        client.post(
            "/predict",
            json={
                "patient_id": "P-002",
                "symptoms": "Patient reports persistent headache, fever of 38.5°C, and stiff neck for 3 days",
                "age": 45,
                "notes": "Hypertensive patient",
            },
        )
    mock_clf.assert_awaited_once()
    _, kwargs = mock_clf.call_args
    assert kwargs["age"] == 45
    assert kwargs["notes"] == "Hypertensive patient"


# ---------------------------------------------------------------------------
# POST /predict — validation errors (422)
# ---------------------------------------------------------------------------


def test_predict_rejects_symptoms_too_short(client: TestClient):
    response = client.post(
        "/predict",
        json={"patient_id": "P-001", "symptoms": "short"},
    )
    assert response.status_code == 422


def test_predict_rejects_blank_symptoms(client: TestClient):
    response = client.post(
        "/predict",
        json={"patient_id": "P-001", "symptoms": "          "},
    )
    assert response.status_code == 422


def test_predict_rejects_missing_patient_id(client: TestClient):
    response = client.post(
        "/predict",
        json={"symptoms": "Patient reports persistent headache and fever for 3 days"},
    )
    assert response.status_code == 422


def test_predict_rejects_age_out_of_range(client: TestClient):
    response = client.post(
        "/predict",
        json={
            "patient_id": "P-001",
            "symptoms": "Patient reports persistent headache and fever for 3 days",
            "age": 200,
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /predict/{record_id}
# ---------------------------------------------------------------------------


def test_get_prediction_returns_record(client: TestClient):
    with patch("app.services.core.get_prediction_by_id", new_callable=AsyncMock, return_value=MOCK_PREDICTION):
        response = client.get("/predict/1")

    assert response.status_code == 200
    assert response.json()["record_id"] == 1


def test_get_prediction_returns_404_when_missing(client: TestClient):
    with patch("app.services.core.get_prediction_by_id", new_callable=AsyncMock, return_value=None):
        response = client.get("/predict/9999")

    assert response.status_code == 404
    assert "9999" in response.json()["detail"]
