"""
Protected-route authentication dependency.

Usage:
    from app.core.security import require_api_key
    from fastapi import Depends

    @router.post("/predict", dependencies=[Depends(require_api_key)])
    async def predict(...): ...

Configuration:
    API_KEYS=key1,key2,key3   (comma-separated; leave blank to disable in dev)

When API_KEYS is empty/unset the dependency is a no-op (dev mode) — all
requests are allowed. When API_KEYS is set, requests must provide either:
    - X-API-Key: <valid key>
    - Authorization: Bearer <valid JWT>
"""

import hashlib
import hmac
import os

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.auth import ALGORITHM, SECRET_KEY, _USERS_DB

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_BEARER_SCHEME = HTTPBearer(auto_error=False)

_VALID_KEYS: set[str] = {
    k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()
}


def _constant_time_compare(a: str, b: str) -> bool:
    """Timing-safe string comparison via HMAC double-hashing."""
    return hmac.compare_digest(
        hashlib.sha256(a.encode()).digest(),
        hashlib.sha256(b.encode()).digest(),
    )


async def require_api_key(
    api_key: str | None = Security(_API_KEY_HEADER),
    bearer: HTTPAuthorizationCredentials | None = Security(_BEARER_SCHEME),
) -> str:
    """FastAPI dependency for protected routes.

    Returns:
        - "dev-mode" when API key auth is disabled
        - the API key string when key auth succeeds
        - "bearer" when JWT auth succeeds

    With API key auth enabled, requires either a valid X-API-Key or a valid
    Bearer JWT.
    """
    if not _VALID_KEYS:
        return "dev-mode"

    if api_key is None and bearer is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "ApiKey, Bearer"},
        )

    if api_key is not None:
        for valid_key in _VALID_KEYS:
            if _constant_time_compare(api_key, valid_key):
                return api_key

    if bearer is not None:
        try:
            payload = jwt.decode(bearer.credentials, SECRET_KEY, algorithms=[ALGORITHM])
            username = payload.get("sub")
            if username and _USERS_DB.get(username):
                return "bearer"
        except JWTError:
            pass

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid authentication credentials",
    )
