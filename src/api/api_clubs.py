import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.routes import User, get_current_admin_user
from db.database import get_db
from db.models import Club, StudentClub

router = APIRouter(tags=["clubs"])


@router.get("/clubs")
async def get_clubs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Club))
    return result.scalars().all()


from pydantic import BaseModel


class StudentClubCreate(BaseModel):
    club_id: uuid.UUID
    role: str
    is_mandat: bool = False


@router.post("/students/{student_id}/clubs")
async def add_student_club(
    student_id: uuid.UUID,
    data: StudentClubCreate,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    sc = StudentClub(
        student_id=student_id,
        club_id=data.club_id,
        role=data.role,
        is_mandat=data.is_mandat,
    )
    db.add(sc)
    await db.commit()
    await db.refresh(sc)
    return sc


@router.delete("/students/{student_id}/clubs/{club_id}")
async def remove_student_club(
    student_id: uuid.UUID,
    club_id: uuid.UUID,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StudentClub).where(
            StudentClub.student_id == student_id, StudentClub.club_id == club_id
        )
    )
    sc = result.scalars().first()
    if not sc:
        raise HTTPException(status_code=404, detail="Student is not in this club")
    await db.delete(sc)
    await db.commit()
    return {"status": "removed"}


class StudentClubUpdate(BaseModel):
    role: str | None = None
    is_mandat: bool | None = None


@router.patch("/students/{student_id}/clubs/{club_id}")
async def update_student_club(
    student_id: uuid.UUID,
    club_id: uuid.UUID,
    data: StudentClubUpdate,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StudentClub).where(
            StudentClub.student_id == student_id, StudentClub.club_id == club_id
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


from sqlalchemy.orm import selectinload


@router.get("/clubs/{club_id}")
async def get_club_details(club_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Club)
        .options(
            selectinload(Club.members).selectinload(StudentClub.student),
            selectinload(Club.events),
            selectinload(Club.links),
        )
        .where(Club.id == club_id)
    )
    club = result.scalars().first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")

    members = []
    for sc in club.members:
        members.append(
            {
                "student_id": str(sc.student.id),
                "first_name": sc.student.first_name,
                "last_name": sc.student.last_name,
                "trombint_id": sc.student.trombint_id,
                "profile_picture_path": sc.student.profile_picture_path,
                "promo": sc.student.promo,
                "ecole": sc.student.ecole,
                "role": sc.role,
                "is_mandat": sc.is_mandat,
            }
        )

    return {
        "id": str(club.id),
        "name": club.name,
        "description": club.description,
        "logo_url": club.logo_url,
        "type": club.type,
        "association_of_origin": club.association_of_origin,
        "color_primary": club.color_primary,
        "color_secondary": club.color_secondary,
        "slug": club.slug,
        "members": members,
        "links": [{"name": link.name, "url": link.url} for link in club.links] if club.links else [],
        "events": [
            {
                "id": str(e.id),
                "name": e.name,
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "room": e.room,
                "description": e.description,
            }
            for e in sorted(club.events, key=lambda ev: ev.start_time)
        ]
        if club.events
        else [],
    }
