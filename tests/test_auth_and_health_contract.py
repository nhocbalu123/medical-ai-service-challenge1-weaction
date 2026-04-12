import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import _USERS_DB, create_access_token
from app.core.security import require_api_key
from app.routers import api
from app.services import core


def test_require_api_key_accepts_valid_x_api_key():
    with patch("app.core.security._VALID_KEYS", {"k1"}):
        result = asyncio.run(require_api_key(api_key="k1", bearer=None))
    assert result == "k1"


def test_require_api_key_accepts_valid_bearer_jwt():
    username = next(iter(_USERS_DB.keys()))
    token = create_access_token({"sub": username})
    bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with patch("app.core.security._VALID_KEYS", {"k1"}):
        result = asyncio.run(require_api_key(api_key=None, bearer=bearer))
    assert result == "bearer"


def test_health_returns_model_name_from_core_constant():
    with (
        patch("app.services.core.check_db_health", new_callable=AsyncMock, return_value=True),
        patch("app.services.core.MODEL_NAME", "custom/model"),
    ):
        body = asyncio.run(api.health())
    assert body["model"] == "custom/model"
    assert body["version"] == core.MODEL_VERSION
