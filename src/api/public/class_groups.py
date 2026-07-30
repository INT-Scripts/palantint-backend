import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.database import get_db
from db.models import Organization

router = APIRouter(tags=["class-groups"])


@router.get("/class-groups")
async def get_class_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Organization).where(Organization.kind == "CLASS_GROUP"))
    # Old `ClassGroup` model only ever had id/name; keep that narrow public shape
    # rather than leaking the richer Organization row (attributes, parent_id, etc).
    return [{"id": str(g.id), "name": g.name} for g in result.scalars().all()]


@router.get("/class-groups/{group_id}")
async def get_class_group_details(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Organization).where(Organization.id == group_id, Organization.kind == "CLASS_GROUP")
    )
    group = result.scalars().first()
    if not group:
        raise HTTPException(status_code=404, detail="Class group not found")

    return {
        "id": str(group.id),
        "name": group.name,
        "members": [],  # stripped for public safety
    }
