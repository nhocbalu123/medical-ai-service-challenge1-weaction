"""Tests for the /health, /predict, and /metrics endpoints."""
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake exception classes used in tests that need asyncpg error types.
#
# conftest.py stubs asyncpg with a MagicMock so the module loads without the
# real package.  Python requires every class listed in an `except` clause to be
# a real BaseException subclass.  We patch asyncpg.PostgresError /
# asyncpg.InterfaceError with these real classes for tests that exercise the
# 503 error path.
# ---------------------------------------------------------------------------
class _FakePostgresError(Exception):
    """Stand-in for asyncpg.PostgresError when asyncpg is mocked."""


class _FakeInterfaceError(Exception):
    """Stand-in for asyncpg.InterfaceError when asyncpg is mocked."""

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
    lifespan startup (init_db + HuggingFace warm-up).
    """
    with (
        patch("app.services.core.init_db", new_callable=AsyncMock),
        patch("app.main._hf_provider._load", return_value=MagicMock()),
    ):
        from app.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_200(client: TestClient):
    from app.services import core

    with patch("app.services.core.check_db_health", new_callable=AsyncMock, return_value=True):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "healthy"
    assert body["model"] == core.MODEL_NAME
    assert body["version"] == core.MODEL_VERSION


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


# ---------------------------------------------------------------------------
# Fallback behaviour (5.0.7)
# ---------------------------------------------------------------------------

FALLBACK_PREDICTION = {
    **MOCK_PREDICTION,
    "top_condition": "unclassifiable",
    "confidence": 0.0,
    "all_predictions": [],
    "is_fallback": True,
    "fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ",
}


def test_predict_returns_201_not_503_when_model_unavailable(client: TestClient):
    """When classify_symptoms returns a fallback dict the API must still return 201."""
    with patch("app.services.core.classify_symptoms", new_callable=AsyncMock, return_value=FALLBACK_PREDICTION):
        response = client.post(
            "/predict",
            json={
                "patient_id": "P-001",
                "symptoms": "Patient reports persistent headache and fever for 3 days",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["is_fallback"] is True
    assert body["top_condition"] == "unclassifiable"
    assert body["confidence"] == 0.0
    assert body["all_predictions"] == []
    assert "fallback_message" in body


def test_predict_fallback_contains_advisory_message(client: TestClient):
    """The fallback_message field must be present and non-empty."""
    with patch("app.services.core.classify_symptoms", new_callable=AsyncMock, return_value=FALLBACK_PREDICTION):
        response = client.post(
            "/predict",
            json={
                "patient_id": "P-003",
                "symptoms": "Patient reports persistent headache and fever for 3 days",
            },
        )

    body = response.json()
    assert body.get("fallback_message"), "fallback_message must be present and non-empty"


def test_predict_normal_result_has_no_fallback_flag(client: TestClient):
    """A successful classification must return is_fallback=False."""
    with patch("app.services.core.classify_symptoms", new_callable=AsyncMock, return_value={
        **MOCK_PREDICTION,
        "is_fallback": False,
        "fallback_message": None,
    }):
        response = client.post(
            "/predict",
            json={
                "patient_id": "P-001",
                "symptoms": "Patient reports persistent headache, fever of 38.5°C, and stiff neck for 3 days",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["is_fallback"] is False
    assert body.get("fallback_message") is None


def test_classify_symptoms_returns_fallback_when_all_providers_fail():
    """Unit test: classify_symptoms returns is_fallback=True when provider chain falls back."""
    import asyncio
    from app.services import core

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value={"id": 99, "created_at": datetime.now(timezone.utc)})
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    fallback_result = {
        "top_condition": "unclassifiable",
        "confidence": 0.0,
        "all_predictions": [],
        "provider": "none",
    }

    async def _run():
        with (
            patch(
                "app.services.core.classify_with_fallback",
                new_callable=AsyncMock,
                return_value=(fallback_result, True),
            ),
            patch("app.services.core.get_db_pool", new_callable=AsyncMock, return_value=mock_pool),
        ):
            return await core.classify_symptoms(
                "P-unit", "Patient has persistent headache and high fever for 3 days", None, None
            )

    result = asyncio.run(_run())

    assert result["is_fallback"] is True
    assert result["top_condition"] == "unclassifiable"
    assert result["confidence"] == 0.0
    assert result["all_predictions"] == []
    assert result["fallback_message"] == core.FALLBACK_MESSAGE


def test_classify_symptoms_returns_non_fallback_result_when_provider_succeeds():
    """Unit test: classify_symptoms returns is_fallback=False when provider chain succeeds."""
    import asyncio
    from app.services import core

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value={"id": 100, "created_at": datetime.now(timezone.utc)})
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    provider_result = {
        "top_condition": "influenza",
        "confidence": 0.88,
        "all_predictions": [{"label": "influenza", "score": 0.88}],
        "provider": "huggingface",
    }

    async def _run():
        with (
            patch(
                "app.services.core.classify_with_fallback",
                new_callable=AsyncMock,
                return_value=(provider_result, False),
            ),
            patch("app.services.core.get_db_pool", new_callable=AsyncMock, return_value=mock_pool),
        ):
            return await core.classify_symptoms(
                "P-unit", "Patient has persistent headache and high fever for 3 days", None, None
            )

    result = asyncio.run(_run())

    assert result["is_fallback"] is False
    assert result["top_condition"] == "influenza"
    assert result["confidence"] == 0.88
    assert result["all_predictions"] == [{"label": "influenza", "score": 0.88}]
    assert "fallback_message" not in result


# ---------------------------------------------------------------------------
# POST /predict — 503 on database error  (HIGH gap)
# ---------------------------------------------------------------------------


def test_predict_returns_503_on_db_error(client: TestClient):
    """Router must return 503 when classify_symptoms raises a DB-related error."""
    asyncpg_stub = sys.modules["asyncpg"]
    with (
        patch.object(asyncpg_stub, "PostgresError", _FakePostgresError),
        patch.object(asyncpg_stub, "InterfaceError", _FakeInterfaceError),
        patch(
            "app.services.core.classify_symptoms",
            new_callable=AsyncMock,
            side_effect=_FakePostgresError("connection pool exhausted"),
        ),
    ):
        response = client.post(
            "/predict",
            json={
                "patient_id": "P-001",
                "symptoms": "Patient reports persistent headache and fever for 3 days",
            },
        )

    assert response.status_code == 503
    assert "Database unavailable" in response.json()["detail"]


def test_predict_returns_503_on_os_error(client: TestClient):
    """Router must return 503 when classify_symptoms raises OSError (e.g. socket failure)."""
    asyncpg_stub = sys.modules["asyncpg"]
    with (
        patch.object(asyncpg_stub, "PostgresError", _FakePostgresError),
        patch.object(asyncpg_stub, "InterfaceError", _FakeInterfaceError),
        patch(
            "app.services.core.classify_symptoms",
            new_callable=AsyncMock,
            side_effect=OSError("broken pipe"),
        ),
    ):
        response = client.post(
            "/predict",
            json={
                "patient_id": "P-001",
                "symptoms": "Patient reports persistent headache and fever for 3 days",
            },
        )

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# GET /predict/{record_id} — additional cases  (HIGH gaps)
# ---------------------------------------------------------------------------


def test_get_prediction_invalid_id_type(client: TestClient):
    """Non-integer path segment must return 422, not 500."""
    response = client.get("/predict/abc")
    assert response.status_code == 422


def test_get_prediction_returns_fallback_message_when_is_fallback(client: TestClient):
    """When the stored record has is_fallback=True the response must include fallback_message."""
    fallback_record = {
        **MOCK_PREDICTION,
        "is_fallback": True,
        "fallback_message": "Không thể phân loại, vui lòng tham khảo bác sĩ",
    }
    with patch("app.services.core.get_prediction_by_id", new_callable=AsyncMock, return_value=fallback_record):
        response = client.get("/predict/1")

    assert response.status_code == 200
    body = response.json()
    assert body["is_fallback"] is True
    assert body["fallback_message"] == "Không thể phân loại, vui lòng tham khảo bác sĩ"


# ---------------------------------------------------------------------------
# POST /predict — boundary validation  (MEDIUM gaps)
# ---------------------------------------------------------------------------


def test_predict_rejects_patient_id_too_long(client: TestClient):
    response = client.post(
        "/predict",
        json={
            "patient_id": "P" * 65,
            "symptoms": "Patient reports persistent headache and fever for 3 days",
        },
    )
    assert response.status_code == 422


def test_predict_rejects_symptoms_too_long(client: TestClient):
    response = client.post(
        "/predict",
        json={
            "patient_id": "P-001",
            "symptoms": "x" * 1001,
        },
    )
    assert response.status_code == 422


def test_predict_rejects_notes_too_long(client: TestClient):
    response = client.post(
        "/predict",
        json={
            "patient_id": "P-001",
            "symptoms": "Patient reports persistent headache and fever for 3 days",
            "notes": "n" * 501,
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /health — metadata remains stable  (MEDIUM gap)
# ---------------------------------------------------------------------------


def test_health_reports_model_metadata_when_db_healthy(client: TestClient):
    """Health endpoint reports configured model metadata."""
    from app.services import core

    with patch("app.services.core.check_db_health", new_callable=AsyncMock, return_value=True):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == core.MODEL_NAME
    assert body["version"] == core.MODEL_VERSION
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# /metrics endpoint  (LOW gap)
# ---------------------------------------------------------------------------


def test_metrics_endpoint_is_reachable(client: TestClient):
    """/metrics must respond 200 with Prometheus text format."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text


# ---------------------------------------------------------------------------
# check_db_health — unit tests  (LOW gaps)
# ---------------------------------------------------------------------------


def test_check_db_health_returns_true_when_db_ok():
    """check_db_health returns True when the pool query succeeds."""
    import asyncio
    from app.services import core

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_conn.fetchval = AsyncMock(return_value=1)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    async def _run():
        with patch("app.services.core.get_db_pool", new_callable=AsyncMock, return_value=mock_pool):
            return await core.check_db_health()

    assert asyncio.run(_run()) is True


def test_check_db_health_returns_false_on_exception():
    """check_db_health returns False (does not raise) when the pool is unavailable."""
    import asyncio
    from app.services import core

    async def _run():
        with patch(
            "app.services.core.get_db_pool",
            new_callable=AsyncMock,
            side_effect=Exception("connection refused"),
        ):
            return await core.check_db_health()

    assert asyncio.run(_run()) is False

