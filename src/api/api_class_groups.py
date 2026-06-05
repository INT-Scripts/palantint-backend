import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.routes import User, get_current_user_optional
from db.database import get_db
from db.models import ClassGroup, StudentClassGroup

router = APIRouter(tags=["class-groups"])


@router.get("/class-groups")
async def get_class_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ClassGroup))
    return result.scalars().all()


@router.get("/class-groups/{group_id}")
async def get_class_group_details(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    result = await db.execute(
        select(ClassGroup)
        .options(
            selectinload(ClassGroup.members).selectinload(StudentClassGroup.student),
        )
        .where(ClassGroup.id == group_id)
    )
    group = result.scalars().first()
    if not group:
        raise HTTPException(status_code=404, detail="Class group not found")

    members = []
    if current_user:
        for sg in group.members:
            if sg.student:
                members.append(
                    {
                        "student_id": str(sg.student.id),
                        "first_name": sg.student.first_name,
                        "last_name": sg.student.last_name,
                        "trombint_id": sg.student.trombint_id,
                        "profile_picture_path": sg.student.profile_picture_path,
                        "promo": sg.student.promo,
                        "ecole": sg.student.ecole,
                        "role": sg.role,
                    }
                )

    return {
        "id": str(group.id),
        "name": group.name,
        "members": members,
    }
