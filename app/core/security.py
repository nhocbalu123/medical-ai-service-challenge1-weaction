"""
X-API-Key authentication dependency.

Usage:
    from app.core.security import require_api_key
    from fastapi import Depends

    @router.post("/predict", dependencies=[Depends(require_api_key)])
    async def predict(...): ...

Configuration:
    API_KEYS=key1,key2,key3   (comma-separated; leave blank to disable in dev)

When API_KEYS is empty/unset the dependency is a no-op — all requests are
allowed.  Set at least one key in any deployed environment.
"""

import hashlib
import hmac
import os

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

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
) -> str:
    """FastAPI dependency — inject into routes that require authentication.

    Returns the validated API key string (or "dev-mode" when auth is disabled).
    Raises HTTP 401 when the header is missing and 403 when the key is invalid.
    """
    if not _VALID_KEYS:
        return "dev-mode"
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    for valid_key in _VALID_KEYS:
        if _constant_time_compare(api_key, valid_key):
            return api_key
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid API key",
    )
