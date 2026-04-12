"""
Authentication router — issues JWT tokens.

POST /auth/token   accepts form-encoded username + password,
                   returns {"access_token": "...", "token_type": "bearer"}.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.auth import _USERS_DB, create_access_token, pwd_context

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/token",
    summary="Obtain a JWT access token",
    description=(
        "Submit `username` and `password` as form data. "
        "Returns a Bearer token valid for `JWT_EXPIRE_MINUTES` minutes."
    ),
)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = _USERS_DB.get(form_data.username)
    if not user or not pwd_context.verify(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(data={"sub": form_data.username})
    return {"access_token": token, "token_type": "bearer"}
