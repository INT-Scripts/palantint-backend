from datetime import timedelta
from jose import jwt, JWTError
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.auth import create_access_token, create_refresh_token, verify_password
from core.config import settings
from core.rate_limit import get_client_ip, login_limiter
from db.database import get_db
from db.models import User

router = APIRouter(tags=["auth"])


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/login")
async def login_for_access_token(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    # Rate limit login attempts (5 per minute per IP)
    login_limiter.check(get_client_ip(request))
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalars().first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)

    access_token = create_access_token(
        data={"sub": user.username, "is_admin": user.is_admin},
        expires_delta=access_token_expires,
    )
    refresh_token = create_refresh_token(
        data={"sub": user.username, "is_admin": user.is_admin},
        expires_delta=refresh_token_expires,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


@router.post("/auth/refresh")
async def refresh_tokens(
    data: RefreshRequest,
    db: AsyncSession = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate refresh credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(data.refresh_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        token_type: str = payload.get("type")
        if username is None or token_type != "refresh":
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)

    access_token = create_access_token(
        data={"sub": user.username, "is_admin": user.is_admin},
        expires_delta=access_token_expires,
    )
    refresh_token = create_refresh_token(
        data={"sub": user.username, "is_admin": user.is_admin},
        expires_delta=refresh_token_expires,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }
