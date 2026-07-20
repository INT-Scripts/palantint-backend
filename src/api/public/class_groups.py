import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.database import get_db
from db.models import ClassGroup

router = APIRouter(tags=["class-groups"])


@router.get("/class-groups")
async def get_class_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ClassGroup))
    return result.scalars().all()


@router.get("/class-groups/{group_id}")
async def get_class_group_details(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ClassGroup).where(ClassGroup.id == group_id)
    )
    group = result.scalars().first()
    if not group:
        raise HTTPException(status_code=404, detail="Class group not found")

    return {
        "id": str(group.id),
        "name": group.name,
        "members": [],  # stripped for public safety
    }
