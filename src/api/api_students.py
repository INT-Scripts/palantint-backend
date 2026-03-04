import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.routes import User, get_current_user, get_current_admin_user
from db.database import get_db
from db.models import SocialLink, Student, StudentClub

router = APIRouter(prefix="/students", tags=["students"])

UPLOAD_PROFILES_DIR = "/app/assets/profiles"
os.makedirs(UPLOAD_PROFILES_DIR, exist_ok=True)


@router.get("/{student_id}/image")
async def get_student_image(student_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    student = await db.get(Student, student_id)
    if not student or not student.trombint_id:
        raise HTTPException(status_code=404, detail="Image not found")

    local_path = os.path.join(UPLOAD_PROFILES_DIR, f"{student.trombint_id}.jpg")

    if os.path.exists(local_path):
        return FileResponse(local_path)

    # Image not yet downloaded — will be available after next sync
    raise HTTPException(
        status_code=404,
        detail="Image not yet downloaded. Run a sync to download images.",
    )


@router.get("/apartments/occupied")
async def get_occupied_apartments(db: AsyncSession = Depends(get_db)):
    # Get all students that have an apartment set
    result = await db.execute(
        select(
            Student.id, Student.first_name, Student.last_name, Student.apartment
        ).where(Student.apartment.isnot(None))
    )

    out = {}
    for row in result.all():
        apt = row.apartment.strip() if row.apartment else None
        if apt:
            if apt not in out:
                out[apt] = []
            out[apt].append(
                {
                    "id": str(row.id),
                    "first_name": row.first_name,
                    "last_name": row.last_name,
                }
            )
    return out


@router.get("")
async def get_students(
    skip: int = 0, limit: int = 24, q: str = None, db: AsyncSession = Depends(get_db)
):
    query = select(Student)
    if q:
        from sqlalchemy import func, or_

        query = query.where(
            or_(
                func.unaccent(Student.first_name).ilike(func.unaccent(f"%{q}%")),
                func.unaccent(Student.last_name).ilike(func.unaccent(f"%{q}%")),
                func.unaccent(Student.promo).ilike(func.unaccent(f"%{q}%")),
                func.unaccent(Student.ecole).ilike(func.unaccent(f"%{q}%")),
            )
        )
    result = await db.execute(query.offset(skip).limit(limit))
    return result.scalars().all()


from db.models import RecentlyViewed


@router.get("/recent")
async def get_recent_students(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(RecentlyViewed)
        .options(selectinload(RecentlyViewed.student))
        .where(RecentlyViewed.user_id == current_user.id)
        .order_by(RecentlyViewed.viewed_at.desc())
        .limit(10)
    )
    rvs = result.scalars().all()
    # Extract just the student objects
    students = [rv.student for rv in rvs if rv.student]
    return students


@router.get("/{student_id}")
async def get_student(student_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Student)
        .options(
            selectinload(Student.social_links),
            selectinload(Student.clubs).selectinload(StudentClub.club),
            selectinload(Student.media),
        )
        .where(Student.id == student_id)
    )
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


from datetime import datetime


@router.post("/{student_id}/recently-viewed")
async def add_recently_viewed(
    student_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    result = await db.execute(
        select(RecentlyViewed).where(
            RecentlyViewed.user_id == current_user.id,
            RecentlyViewed.student_id == student_id,
        )
    )
    rv = result.scalars().first()

    if rv:
        rv.viewed_at = datetime.utcnow()
    else:
        rv = RecentlyViewed(user_id=current_user.id, student_id=student_id)
        db.add(rv)

    await db.commit()
    return {"status": "ok"}


from pydantic import BaseModel


class SocialLinkCreate(BaseModel):
    platform: str
    username: str
    url: str


@router.post("/{student_id}/socials")
async def add_social_link(
    student_id: uuid.UUID,
    social: SocialLinkCreate,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    new_social = SocialLink(
        student_id=student_id,
        platform=social.platform,
        username=social.username,
        url=social.url,
    )
    db.add(new_social)
    await db.commit()
    await db.refresh(new_social)
    return new_social


@router.delete("/{student_id}/socials/{social_id}")
async def delete_social_link(
    student_id: uuid.UUID,
    social_id: uuid.UUID,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    social = await db.get(SocialLink, social_id)
    if not social or social.student_id != student_id:
        raise HTTPException(status_code=404, detail="Social link not found")
    await db.delete(social)
    await db.commit()
    return {"status": "ok"}


class StudentUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    promo: str | None = None
    ecole: str | None = None
    email: str | None = None


@router.patch("/{student_id}")
async def update_student(
    student_id: uuid.UUID,
    data: StudentUpdate,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(student, field, value)

    await db.commit()
    await db.refresh(student)
    return student
