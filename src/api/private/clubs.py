import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.common import (
    CLUB_KINDS,
    build_foyer_index,
    club_type,
    get_active_promo_school,
    get_data_source_id,
    get_trombint_id,
)
from api.private.deps import User, require_admin, require_user
from db.database import get_db
from db.models import Event, EventOrganization, Organization, OrganizationMembership

router = APIRouter(tags=["clubs"])


class StudentClubCreate(BaseModel):
    club_id: uuid.UUID
    role: str
    is_mandat: bool = False


class StudentClubUpdate(BaseModel):
    role: str | None = None
    is_mandat: bool | None = None


@router.post("/students/{student_id}/clubs")
async def add_student_club(
    student_id: uuid.UUID,
    data: StudentClubCreate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == student_id,
            OrganizationMembership.organization_id == data.club_id,
            OrganizationMembership.ended_at.is_(None),
        )
    )
    existing = result.scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Student is already in this club")

    source_id = await get_data_source_id(db, "admin_panel")
    sc = OrganizationMembership(
        person_id=student_id,
        organization_id=data.club_id,
        role=data.role,
        is_mandat=data.is_mandat,
        source_id=source_id,
    )
    db.add(sc)
    await db.commit()
    await db.refresh(sc)
    return sc


@router.delete("/students/{student_id}/clubs/{club_id}")
async def remove_student_club(
    student_id: uuid.UUID,
    club_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == student_id,
            OrganizationMembership.organization_id == club_id,
            OrganizationMembership.ended_at.is_(None),
        )
    )
    sc = result.scalars().first()
    if not sc:
        raise HTTPException(status_code=404, detail="Student is not in this club")
    # Close out the membership rather than delete, to preserve history.
    sc.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return {"status": "removed"}


@router.patch("/students/{student_id}/clubs/{club_id}")
async def update_student_club(
    student_id: uuid.UUID,
    club_id: uuid.UUID,
    data: StudentClubUpdate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == student_id,
            OrganizationMembership.organization_id == club_id,
            OrganizationMembership.ended_at.is_(None),
        )
    )
    sc = result.scalars().first()
    if not sc:
        raise HTTPException(status_code=404, detail="Student is not in this club")

    if data.role is not None:
        sc.role = data.role
    if data.is_mandat is not None:
        sc.is_mandat = data.is_mandat

    await db.commit()
    await db.refresh(sc)
    return sc


@router.get("/clubs/{club_id}")
async def get_club_details(
    club_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    result = await db.execute(
        select(Organization)
        .options(
            selectinload(Organization.members).selectinload(OrganizationMembership.person),
            selectinload(Organization.links),
        )
        .where(Organization.id == club_id, Organization.kind.in_(CLUB_KINDS))
    )
    club = result.scalars().first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")

    _, room_id_by_club_id = await build_foyer_index(db)
    foyer_room_code = room_id_by_club_id.get(club.id)

    events_result = await db.execute(
        select(Event)
        .options(selectinload(Event.location))
        .join(EventOrganization, EventOrganization.event_id == Event.id)
        .where(EventOrganization.organization_id == club.id)
    )
    club_events = events_result.scalars().all()
    if not club_events:
        # Fall back to the single primary organization link (most club events
        # are created with Event.organization_id set directly).
        events_result = await db.execute(
            select(Event).options(selectinload(Event.location)).where(Event.organization_id == club.id)
        )
        club_events = events_result.scalars().all()

    members = []
    for m in club.members:
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
                "is_mandat": m.is_mandat,
            }
        )

    return {
        "id": str(club.id),
        "name": club.name,
        "description": club.description,
        "logo_url": club.logo_url,
        "type": club_type(club),
        "association_of_origin": club.attributes.get("association_of_origin"),
        "color_primary": club.color_primary,
        "color_secondary": club.color_secondary,
        "foyer_room": foyer_room_code,
        "slug": club.slug,
        "members": members,
        "links": [{"name": link.name, "url": link.url} for link in club.links] if club.links else [],
        "events": [
            {
                "id": str(e.id),
                "name": e.name,
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "room": (e.location.name or e.location.code) if e.location else None,
                "description": e.description,
            }
            for e in sorted(club_events, key=lambda ev: ev.start_time)
        ],
    }
