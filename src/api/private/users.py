from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.private.deps import require_user
from db.database import get_db
from db.models import Student, User

router = APIRouter(prefix="/users", tags=["users"])


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


@router.get("/me")
async def read_users_me(current_user: User = Depends(require_user)):
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "is_admin": current_user.is_admin,
    }
