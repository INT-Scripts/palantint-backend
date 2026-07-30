import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.common import get_active_promo_school, get_trombint_id
from api.private.deps import User, require_user
from db.database import get_db
from db.models import Organization, OrganizationMembership

router = APIRouter(tags=["class-groups"])


@router.get("/class-groups")
async def get_class_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """List all class groups. Was missing from the private router entirely —
    mcp_server.py's get_class_roster tool has been calling this path since
    before this schema migration and always 404ing."""
    result = await db.execute(select(Organization).where(Organization.kind == "CLASS_GROUP"))
    return [{"id": str(g.id), "name": g.name} for g in result.scalars().all()]


@router.get("/class-groups/{group_id}")
async def get_class_group_details(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    result = await db.execute(
        select(Organization)
        .options(
            selectinload(Organization.members).selectinload(OrganizationMembership.person),
        )
        .where(Organization.id == group_id, Organization.kind == "CLASS_GROUP")
    )
    group = result.scalars().first()
    if not group:
        raise HTTPException(status_code=404, detail="Class group not found")

    members = []
    for m in group.members:
        if m.ended_at is not None or not m.person:
            continue
        student = m.person
        trombint_id = await get_trombint_id(db, student.id)
        promo, ecole = await get_active_promo_school(db, student.id)
        members.append(
            {
                "student_id": str(student.id),
                "first_name": student.first_name,
                "last_name": student.last_name,
                "trombint_id": trombint_id,
                "profile_picture_path": student.profile_picture_path,
                "promo": promo,
                "ecole": ecole,
                "role": m.role,
            }
        )

    return {
        "id": str(group.id),
        "name": group.name,
        "members": members,
    }
