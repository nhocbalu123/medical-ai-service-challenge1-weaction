"""
JWT authentication helpers.

This is a demo-grade implementation: users are stored in memory, seeded from
environment variables.  Replace _USERS_DB with a real database query before
deploying to production.

Configuration:
    JWT_SECRET_KEY        — required in production (generate with: openssl rand -hex 32)
    JWT_EXPIRE_MINUTES    — token lifetime, default 60
    ADMIN_USERNAME        — default "admin"
    ADMIN_PASSWORD        — default "changeme" (CHANGE THIS)
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "insecure-dev-secret-change-in-production")
ALGORITHM = "HS256"
EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

# In-memory user store — swap for DB lookup in production.
_USERS_DB: dict[str, dict] = {
    os.getenv("ADMIN_USERNAME", "admin"): {
        "hashed_password": pwd_context.hash(
            os.getenv("ADMIN_PASSWORD", "changeme")
        ),
        "role": "admin",
    }
}


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
) -> dict:
    """FastAPI dependency — validates a Bearer JWT and returns the user dict."""
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str | None = payload.get("sub")
        if username is None:
            raise exc
    except JWTError:
        raise exc
    user = _USERS_DB.get(username)
    if user is None:
        raise exc
    return {"username": username, **user}
