import uuid

from fastapi import Depends, HTTPException, Query, status, Cookie
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.auth import TokenData
from core.config import settings
from db.database import get_db
from db.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


async def _resolve_user_from_token(
    token_value: str | None,
    db: AsyncSession,
) -> User:
    """Shared logic: decode a JWT string and return the User or raise 401."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token_value:
        raise credentials_exception

    # Support Internal Service Token (MCP)
    if settings.MCP_SERVICE_TOKEN and token_value == settings.MCP_SERVICE_TOKEN:
        # Return a virtual 'system' admin user
        return User(
            id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            username="system_mcp",
            hashed_password="[INTERNAL]",
            is_admin=True
        )

    try:
        payload = jwt.decode(token_value, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        token_type: str = payload.get("type", "access")
        if username is None or token_type != "access":
            raise credentials_exception
        token_data = TokenData(
            username=username, is_admin=payload.get("is_admin", False)
        )
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.username == token_data.username))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
    return user


async def require_user(
    token: str | None = Depends(oauth2_scheme),
    palantint_token: str | None = Cookie(None),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Standard auth: Bearer header or cookie. Formerly get_current_user."""
    return await _resolve_user_from_token(token or palantint_token, db)


async def require_user_query_token(
    token: str | None = Depends(oauth2_scheme),
    token_query: str | None = Query(None, alias="token"),
    palantint_token: str | None = Cookie(None),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Auth for <img src> endpoints: accepts Bearer header OR ?token= query param OR cookie. Formerly get_current_user_with_query_token."""
    return await _resolve_user_from_token(token or token_query or palantint_token, db)


async def require_admin(current_user: User = Depends(require_user)) -> User:
    """Replaces get_current_admin_user."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403, detail="The user doesn't have enough privileges"
        )
    return current_user


async def optional_user(
    token: str | None = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> User | None:
    """Replaces get_current_user_optional."""
    if not token:
        return None
    
    # Support Internal Service Token (MCP) - allows student search
    if settings.MCP_SERVICE_TOKEN and token == settings.MCP_SERVICE_TOKEN:
        return User(
            id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            username="system_mcp",
            hashed_password="[INTERNAL]",
            is_admin=True
        )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
    except JWTError:
        return None

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    return user


def escape_like(term: str) -> str:
    """Escape LIKE-special characters (%, _) to prevent wildcard abuse."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
