import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased, selectinload

from api.common import (
    apartment_code_subquery,
    get_active_housing,
    get_trombint_id,
    promo_name_subquery,
    school_name_subquery,
    trombint_id_subquery,
)
from api.private.deps import (
    User,
    escape_like,
    require_admin,
    require_user,
    require_user_query_token,
)
from core.config import settings
from db.database import get_db
from db.models import (
    Location,
    Organization,
    OrganizationMembership,
    Person,
    RecentlyViewed,
    SocialLink,
    utc_now,
)

router = APIRouter(prefix="/students", tags=["students"])

UPLOAD_PROFILES_DIR = settings.PROFILES_DIR


class SocialLinkCreate(BaseModel):
    platform: str
    username: str
    url: str


class StudentUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None


@router.get("/{student_id}/image")
async def get_student_image(
    student_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user_query_token),
):
    student = await db.get(Person, student_id)
    trombint_id = await get_trombint_id(db, student_id) if student else None
    if not student or not trombint_id:
        raise HTTPException(status_code=404, detail="Image not found")

    extensions = [".jpg", ".jpeg", ".png", ".webp", ".svg", ".gif"]
    for ext in extensions:
        local_path = os.path.join(UPLOAD_PROFILES_DIR, f"{trombint_id}{ext}")
        if os.path.exists(local_path):
            return FileResponse(local_path)

    raise HTTPException(
        status_code=404,
        detail="Image not yet downloaded. Run a sync to download images.",
    )


@router.get("/apartments/details")
async def get_apartment_details(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Was missing from the private router entirely — mcp_server.py's
    get_apartment_info tool has been calling this path since before this
    schema migration and always 404ing. Mirrors api/public/students.py."""
    result = await db.execute(
        select(Location).where(Location.kind == "APARTMENT").options(selectinload(Location.parent))
    )
    details = result.scalars().all()

    out = {}
    for d in details:
        attrs = d.attributes or {}
        out[d.code] = {
            "Logement": d.code,
            "Bâtiment": d.parent.code if d.parent else attrs.get("building"),
            "Etage": attrs.get("floor"),
            "Type": attrs.get("type"),
            "Superficie": attrs.get("surface"),
            "Tarif": attrs.get("price"),
            "Allocation boursier": attrs.get("alloc_boursier"),
            "Allocation non boursier": attrs.get("alloc_non_boursier"),
            "_req_b": attrs.get("req_b"),
            "_req_e": attrs.get("req_e"),
        }
    return out


@router.get("/apartments/occupied")
async def get_occupied_apartments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    result = await db.execute(
        select(
            Person.id,
            Person.first_name,
            Person.last_name,
            apartment_code_subquery(Person.id).label("apartment"),
        ).where(Person.kind == "STUDENT")
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


@router.get("/filters")
async def get_student_filters(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    promos_result = await db.execute(
        select(Organization.name).where(Organization.kind == "PROMO")
    )
    ecoles_result = await db.execute(
        select(Organization.name).where(Organization.kind == "SCHOOL")
    )

    promos = sorted([p for p in promos_result.scalars().all() if p])
    ecoles = sorted([e for e in ecoles_result.scalars().all() if e])

    return {"promos": promos, "ecoles": ecoles}


@router.get("")
async def get_students(
    skip: int = 0,
    limit: int = 24,
    q: str = None,
    promo: str = None,
    ecole: str = None,
    bldg: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    promo_expr = promo_name_subquery(Person.id)
    school_expr = school_name_subquery(Person.id)
    apartment_expr = apartment_code_subquery(Person.id)

    query = select(
        Person,
        promo_expr.label("promo"),
        school_expr.label("ecole"),
        apartment_expr.label("apartment"),
        trombint_id_subquery(Person.id).label("trombint_id"),
    ).where(Person.kind == "STUDENT")

    if q:
        sq = escape_like(q)
        query = query.where(
            or_(
                func.unaccent(Person.first_name).ilike(func.unaccent(f"%{sq}%")),
                func.unaccent(Person.last_name).ilike(func.unaccent(f"%{sq}%")),
                func.unaccent(promo_expr).ilike(func.unaccent(f"%{sq}%")),
                func.unaccent(school_expr).ilike(func.unaccent(f"%{sq}%")),
            )
        )

    if promo:
        query = query.where(promo_expr == promo)

    if ecole:
        query = query.where(school_expr == ecole)

    if bldg:
        bldg_digit = bldg.replace("U", "").strip()
        if bldg_digit.isdigit():
            query = query.where(apartment_expr.like(f"{bldg_digit}%"))

    result = await db.execute(query.offset(skip).limit(limit).order_by(Person.last_name))

    students = []
    for person, promo_name, ecole_name, apartment, trombint_id in result.all():
        students.append({
            "id": str(person.id),
            "trombint_id": trombint_id,
            "first_name": person.first_name,
            "last_name": person.last_name,
            "email": person.email,
            "promo": promo_name,
            "ecole": ecole_name,
            "apartment": apartment,
            "profile_picture_path": person.profile_picture_path,
        })
    return students


@router.get("/recent")
async def get_recent_students(
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(RecentlyViewed)
        .options(selectinload(RecentlyViewed.person))
        .where(RecentlyViewed.user_id == current_user.id)
        .order_by(RecentlyViewed.viewed_at.desc())
        .limit(10)
    )
    rvs = result.scalars().all()
    return [rv.person for rv in rvs if rv.person]


@router.get("/{student_id}")
async def get_student(
    student_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    result = await db.execute(
        select(Person)
        .options(
            selectinload(Person.social_links),
            selectinload(Person.memberships).selectinload(OrganizationMembership.organization),
            selectinload(Person.media),
        )
        .where(Person.id == student_id)
    )
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    trombint_id = await get_trombint_id(db, student_id)
    housing = await get_active_housing(db, student_id)

    active_memberships = [m for m in student.memberships if m.ended_at is None and m.organization]
    clubs = [m for m in active_memberships if m.organization.kind in ("CLUB", "BUREAU")]
    class_groups = [m for m in active_memberships if m.organization.kind == "CLASS_GROUP"]
    promo_membership = next((m for m in active_memberships if m.organization.kind == "PROMO"), None)
    school_membership = None
    if promo_membership and promo_membership.organization.parent_id:
        school_membership = await db.get(Organization, promo_membership.organization.parent_id)

    return {
        "id": str(student.id),
        "trombint_id": trombint_id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "email": student.email,
        "promo": promo_membership.organization.name if promo_membership else None,
        "ecole": school_membership.name if school_membership else None,
        "apartment": housing.code if housing else None,
        "housing": {
            "location_id": str(housing.id),
            "code": housing.code,
            "attributes": housing.attributes,
        } if housing else None,
        "social_links": [
            {
                "id": str(s.id),
                "platform": s.platform,
                "username": s.username,
                "url": s.url
            } for s in student.social_links
        ],
        "clubs": [
            {
                "club_id": str(m.organization.id),
                "club_name": m.organization.name,
                "club_type": m.organization.kind,
                "role": m.role,
                "is_mandat": m.is_mandat,
                "club": {
                    "id": str(m.organization.id),
                    "name": m.organization.name,
                    "type": m.organization.kind,
                    "logo_url": m.organization.logo_url,
                }
            } for m in clubs
        ],
        "class_groups": [
            {
                "class_group_id": str(m.organization.id),
                "class_group_name": m.organization.name,
                "role": m.role,
                "class_group": {
                    "id": str(m.organization.id),
                    "name": m.organization.name,
                }
            } for m in class_groups
        ],
        "media": [
            {
                "id": str(m.id),
                "type": m.kind,
                "file_path": m.file_path,
                "content": m.content,
                "author_name": m.author_name,
                "uploaded_at": m.uploaded_at.isoformat()
            } for m in student.media
        ]
    }


@router.post("/{student_id}/recently-viewed")
async def add_recently_viewed(
    student_id: uuid.UUID,
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Person, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    result = await db.execute(
        select(RecentlyViewed).where(
            RecentlyViewed.user_id == current_user.id,
            RecentlyViewed.person_id == student_id,
        )
    )
    rv = result.scalars().first()

    if rv:
        rv.viewed_at = utc_now()
    else:
        rv = RecentlyViewed(user_id=current_user.id, person_id=student_id)
        db.add(rv)

    await db.commit()
    return {"status": "ok"}


@router.post("/{student_id}/socials")
async def add_social_link(
    student_id: uuid.UUID,
    social: SocialLinkCreate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Person, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    new_social = SocialLink(
        person_id=student_id,
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
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    social = await db.get(SocialLink, social_id)
    if not social or social.person_id != student_id:
        raise HTTPException(status_code=404, detail="Social link not found")
    await db.delete(social)
    await db.commit()
    return {"status": "ok"}


@router.patch("/{student_id}")
async def update_student(
    student_id: uuid.UUID,
    data: StudentUpdate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Person, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(student, field, value)

    await db.commit()
    await db.refresh(student)
    return student
