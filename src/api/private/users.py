from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.private.deps import require_user
from db.database import get_db
from db.models import Student, User

from pydantic import BaseModel

router = APIRouter(prefix="/users", tags=["users"])


class CasCredentialsSchema(BaseModel):
    cas_username: str
    cas_password: str


@router.get("/me/student")
async def get_my_student_profile(
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Student).where(Student.trombint_id == current_user.username))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found for this user")
    return student


@router.get("/me/cas-credentials")
async def get_my_cas_credentials(current_user: User = Depends(require_user)):
    return {
        "has_credentials": bool(current_user.cas_username and current_user.cas_password),
        "cas_username": current_user.cas_username or ""
    }


@router.post("/me/cas-credentials")
async def save_my_cas_credentials(
    credentials: CasCredentialsSchema,
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    current_user.cas_username = credentials.cas_username
    current_user.cas_password = credentials.cas_password
    db.add(current_user)
    await db.commit()
    return {"status": "success"}


@router.get("/me")
async def read_users_me(current_user: User = Depends(require_user)):
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "is_admin": current_user.is_admin,
    }
