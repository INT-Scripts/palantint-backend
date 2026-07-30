import csv
import io
import secrets
import string
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from api.common import (
    apartment_code_subquery,
    get_data_source_id,
    promo_name_subquery,
    school_name_subquery,
    trombint_id_subquery,
)
from api.private.deps import escape_like, require_admin
from db.database import get_db
from db.models import (
    DataSource,
    Event,
    ExternalIdentity,
    Location,
    Organization,
    OrganizationMembership,
    Person,
    PersonHousing,
    RelationshipType,
    User,
    utc_now,
)

router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_SOURCE_CODE = "admin_panel"
TROMBINT_SOURCE_CODE = "trombint"


class ApartmentUpdate(BaseModel):
    apartment: str | None


class StudentPatch(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    ecole: str | None = None
    promo: str | None = None
    apartment: str | None = None


class ProvisionRequest(BaseModel):
    student_id: uuid.UUID
    is_admin: bool = False


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserUpdate(BaseModel):
    is_admin: bool


class RelationshipTypeCreate(BaseModel):
    name: str
    color: str = "#cccccc"


# ── Internal helpers ──────────────────────────────────────────────────────────
# The old schema had flat Student.apartment / .ecole / .promo string columns
# that admin endpoints just overwrote. The new schema models those as
# history-tracked relations (PersonHousing, OrganizationMembership), so admin
# writes here "close out the active row and open a new one" instead of a bare
# assignment. See migration briefing for the mapping.

async def _get_or_create_apartment_location(db: AsyncSession, code: str) -> Location:
    code = code.strip()
    result = await db.execute(
        select(Location).where(Location.kind == "APARTMENT", Location.code == code)
    )
    location = result.scalars().first()
    if not location:
        location = Location(kind="APARTMENT", code=code)
        db.add(location)
        await db.flush()
    return location


async def _assign_housing(
    db: AsyncSession,
    person_id: uuid.UUID,
    apartment_code: str | None,
    source_id: uuid.UUID | None,
) -> None:
    """Replaces `student.apartment = value`. Closes any currently-active
    PersonHousing row for this person, then (if a code was given) opens a new
    one pointing at the matching APARTMENT Location, so history is preserved
    instead of destructively overwritten."""
    code = apartment_code.strip() if apartment_code else None

    result = await db.execute(
        select(PersonHousing).where(
            PersonHousing.person_id == person_id,
            PersonHousing.ended_at.is_(None),
        )
    )
    active = result.scalars().first()

    if active:
        if code:
            current_location = await db.get(Location, active.location_id)
            if current_location and current_location.code == code:
                return  # already assigned to this apartment, no-op
        active.ended_at = utc_now()

    if code:
        location = await _get_or_create_apartment_location(db, code)
        db.add(
            PersonHousing(person_id=person_id, location_id=location.id, source_id=source_id)
        )


async def _get_or_create_organization(
    db: AsyncSession, kind: str, name: str, parent_id: uuid.UUID | None = None
) -> Organization:
    name = name.strip()
    result = await db.execute(
        select(Organization).where(Organization.kind == kind, Organization.name == name)
    )
    org = result.scalars().first()
    if not org:
        org = Organization(kind=kind, name=name, parent_id=parent_id)
        db.add(org)
        await db.flush()
    return org


async def _get_active_promo_membership(
    db: AsyncSession, person_id: uuid.UUID
) -> tuple[OrganizationMembership | None, Organization | None]:
    result = await db.execute(
        select(OrganizationMembership, Organization)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.person_id == person_id,
            OrganizationMembership.ended_at.is_(None),
            Organization.kind == "PROMO",
        )
    )
    row = result.first()
    return (row[0], row[1]) if row else (None, None)


async def _assign_promo(
    db: AsyncSession, person_id: uuid.UUID, promo_name: str | None, source_id: uuid.UUID | None
) -> None:
    """Replaces `student.promo = value`. Closes the active PROMO
    OrganizationMembership and opens a new one against a find-or-created
    Organization(kind="PROMO")."""
    name = promo_name.strip() if promo_name else None
    membership, org = await _get_active_promo_membership(db, person_id)

    if membership:
        if name and org and org.name == name:
            return  # unchanged
        membership.ended_at = utc_now()

    if name:
        promo_org = await _get_or_create_organization(db, "PROMO", name)
        db.add(
            OrganizationMembership(
                person_id=person_id,
                organization_id=promo_org.id,
                role="Membre",
                source_id=source_id,
            )
        )


async def _assign_ecole(
    db: AsyncSession, person_id: uuid.UUID, ecole_name: str | None
) -> None:
    """Replaces `student.ecole = value`.

    JUDGMENT CALL: in the new schema "ecole" isn't a per-student field at all
    -- it's derived from the person's active PROMO organization's parent
    (SCHOOL) organization. There's no way to give one student a different
    school than the rest of their promo without either forking the promo or
    attaching the school elsewhere, and the old admin grid never did that
    (editing "ecole" and "promo" were two independent flat columns on one
    row). Closest faithful behavior: if the person has an active promo
    membership, retarget *that promo's* parent to the (find-or-created)
    SCHOOL org -- i.e. "this promo belongs to this school", which is what the
    field conceptually meant. If the person has no active promo, there's
    nothing sensible to attach the school to, so this is a no-op.
    """
    name = ecole_name.strip() if ecole_name else None
    _membership, promo_org = await _get_active_promo_membership(db, person_id)
    if not promo_org:
        return

    if not name:
        promo_org.parent_id = None
        return

    school_org = await _get_or_create_organization(db, "SCHOOL", name)
    promo_org.parent_id = school_org.id


@router.get("/telemetry")
async def get_system_telemetry(
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    students_count = await db.scalar(
        select(func.count(Person.id)).where(Person.kind == "STUDENT")
    )
    clubs_count = await db.scalar(
        select(func.count(Organization.id)).where(Organization.kind == "CLUB")
    )
    events_count = await db.scalar(select(func.count(Event.id)))
    users_count = await db.scalar(select(func.count(User.id)))

    return {
        "status": "online",
        "counts": {
            "students": students_count,
            "clubs": clubs_count,
            "events": events_count,
            "users": users_count
        }
    }


@router.patch("/students/{student_id}/apartment")
async def update_student_apartment(
    student_id: uuid.UUID,
    data: ApartmentUpdate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    student = await db.get(Person, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    admin_source_id = await get_data_source_id(db, ADMIN_SOURCE_CODE)
    await _assign_housing(db, student_id, data.apartment, admin_source_id)
    await db.commit()

    result = await db.execute(
        select(apartment_code_subquery(Person.id)).where(Person.id == student_id)
    )
    apartment = result.scalar_one_or_none()
    return {"status": "success", "apartment": apartment}


@router.get("/students/grid")
async def get_students_grid(
    skip: int = 0,
    limit: int = 100,
    search: str = "",
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    trombint_source_id = await get_data_source_id(db, TROMBINT_SOURCE_CODE)
    TrombintIdentity = aliased(ExternalIdentity)

    trombint_expr = TrombintIdentity.external_id

    query = (
        select(
            Person,
            User,
            trombint_expr.label("trombint_id"),
            promo_name_subquery(Person.id).label("promo"),
            school_name_subquery(Person.id).label("ecole"),
            apartment_code_subquery(Person.id).label("apartment"),
        )
        .where(Person.kind == "STUDENT")
        .outerjoin(
            TrombintIdentity,
            and_(
                TrombintIdentity.person_id == Person.id,
                TrombintIdentity.source_id == trombint_source_id,
            ),
        )
        .outerjoin(User, User.username == trombint_expr)
    )

    if search:
        s = escape_like(search)
        query = query.where(
            or_(
                Person.first_name.ilike(f"%{s}%"),
                Person.last_name.ilike(f"%{s}%"),
                trombint_expr.ilike(f"%{s}%"),
            )
        )

    query = query.order_by(Person.last_name).offset(skip).limit(limit)

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": str(person.id),
            "trombint_id": trombint_id,
            "first_name": person.first_name,
            "last_name": person.last_name,
            "ecole": ecole,
            "promo": promo,
            "apartment": apartment,
            "is_active": person.is_active,
            "user_id": str(user.id) if user else None,
            "is_admin": user.is_admin if user else False
        }
        for person, user, trombint_id, promo, ecole, apartment in rows
    ]


@router.patch("/students/{student_id}")
async def patch_student_grid(
    student_id: uuid.UUID,
    data: StudentPatch,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    student = await db.get(Person, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    admin_source_id = await get_data_source_id(db, ADMIN_SOURCE_CODE)
    update_data = data.model_dump(exclude_unset=True)

    if "first_name" in update_data:
        student.first_name = update_data["first_name"]
    if "last_name" in update_data:
        student.last_name = update_data["last_name"]
    if "apartment" in update_data:
        await _assign_housing(db, student_id, update_data["apartment"], admin_source_id)
    if "promo" in update_data:
        await _assign_promo(db, student_id, update_data["promo"], admin_source_id)
    if "ecole" in update_data:
        await _assign_ecole(db, student_id, update_data["ecole"])

    await db.commit()
    return {"status": "success"}


@router.post("/users/provision")
async def provision_user(
    req: ProvisionRequest,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    from core.auth import get_password_hash

    student = await db.get(Person, req.student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    trombint_result = await db.execute(
        select(ExternalIdentity.external_id)
        .join(DataSource, DataSource.id == ExternalIdentity.source_id)
        .where(
            ExternalIdentity.person_id == req.student_id,
            DataSource.code == TROMBINT_SOURCE_CODE,
        )
    )
    trombint_id = trombint_result.scalar_one_or_none()
    if not trombint_id:
        raise HTTPException(status_code=400, detail="Student has no trombint identity to provision a login for")

    user_check = await db.execute(select(User).where(User.username == trombint_id))
    if user_check.scalars().first():
        raise HTTPException(status_code=400, detail="User already provisioned for this student")

    # NOTE: this only creates the platform login (User row) with a random
    # generated password -- it does NOT touch CAS credentials. CAS creds are
    # a separate self-service flow (see api/private/users.py's
    # /users/me/cas-credentials, backed by UserCredential); the old
    # provisioning flow here never touched User.cas_username/cas_password
    # either, so there's no UserCredential row to create in this endpoint.
    alphabet = string.ascii_letters + string.digits
    password = ''.join(secrets.choice(alphabet) for i in range(12))

    db_user = User(
        username=trombint_id,
        hashed_password=get_password_hash(password),
        is_admin=req.is_admin
    )
    db.add(db_user)
    await db.commit()

    return JSONResponse(
        content={
            "status": "success",
            "user_id": str(db_user.id),
            "username": db_user.username,
            "generated_password": password,
            "is_admin": db_user.is_admin
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/students/search")
async def search_students_admin(
    q: str = "",
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    if not q or len(q) < 2:
        return []

    sq = escape_like(q)
    query = (
        select(
            Person,
            trombint_id_subquery(Person.id).label("trombint_id"),
            apartment_code_subquery(Person.id).label("apartment"),
        )
        .where(
            Person.kind == "STUDENT",
            or_(
                Person.first_name.ilike(f"%{sq}%"),
                Person.last_name.ilike(f"%{sq}%"),
                trombint_id_subquery(Person.id).ilike(f"%{sq}%"),
            ),
        )
        .limit(10)
    )

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": str(s.id),
            "first_name": s.first_name,
            "last_name": s.last_name,
            "trombint_id": trombint_id,
            "apartment": apartment
        }
        for s, trombint_id, apartment in rows
    ]


@router.post("/users", response_model=dict)
async def create_user(
    user: UserCreate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from core.auth import get_password_hash

    result = await db.execute(select(User).where(User.username == user.username))
    db_user = result.scalars().first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")

    hashed_password = get_password_hash(user.password)
    db_user = User(
        username=user.username, hashed_password=hashed_password, is_admin=user.is_admin
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return {
        "id": str(db_user.id),
        "username": db_user.username,
        "is_admin": db_user.is_admin,
    }


@router.get("/users")
async def get_all_users(
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User))
    users = result.scalars().all()
    return [
        {"id": str(u.id), "username": u.username, "is_admin": u.is_admin} for u in users
    ]


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_admin = data.is_admin
    await db.commit()
    return {"id": str(user.id), "username": user.username, "is_admin": user.is_admin}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.delete(user)
    await db.commit()
    return {"status": "success", "message": "User deleted"}


@router.get("/apartments/template")
async def download_apartment_template(
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Person, trombint_id_subquery(Person.id).label("trombint_id"))
        .where(Person.kind == "STUDENT")
        .order_by(Person.last_name, Person.first_name)
    )
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["nom", "prénom", "id utilisateur", "num appart"])

    for s, trombint_id in rows:
        writer.writerow(
            [s.last_name or "", s.first_name or "", trombint_id or s.id, ""]
        )

    content_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        content=content_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=apartment_template.csv"},
    )


@router.post("/apartments/upload")
async def upload_apartments_csv(
    file: UploadFile = File(...),
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Must be a CSV file")

    content = await file.read()
    try:
        decoded_content = content.decode("utf-8-sig")
    except Exception:
        decoded_content = content.decode("latin-1")

    first_line = decoded_content.split("\n")[0] if decoded_content else ""
    delimiter = ";" if ";" in first_line else ","

    reader = csv.DictReader(io.StringIO(decoded_content), delimiter=delimiter)

    admin_source_id = await get_data_source_id(db, ADMIN_SOURCE_CODE)

    updated_count = 0
    not_found = []

    for row in reader:
        user_id = row.get("id utilisateur") or row.get("trombint_id") or row.get("id")
        apartment_num = (
            row.get("num appart") or row.get("numero appart") or row.get("apartment")
        )

        if not user_id:
            continue

        normalized_id = user_id.strip()
        if normalized_id.startswith("@"):
            normalized_id = normalized_id[1:]

        result = await db.execute(
            select(ExternalIdentity.person_id)
            .join(DataSource, DataSource.id == ExternalIdentity.source_id)
            .where(
                DataSource.code == TROMBINT_SOURCE_CODE,
                ExternalIdentity.external_id == normalized_id,
            )
        )
        person_id = result.scalar_one_or_none()

        if person_id:
            await _assign_housing(
                db, person_id, apartment_num.strip() if apartment_num else None, admin_source_id
            )
            updated_count += 1
        else:
            not_found.append(user_id)

    await db.commit()

    msg = f"Successfully updated {updated_count} students."
    if not_found:
        msg += f" {len(not_found)} users not found."
    return {
        "status": "success",
        "message": msg,
        "updated": updated_count,
        "not_found": not_found,
    }


@router.post("/relationship-types")
async def create_relationship_type(
    data: RelationshipTypeCreate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rt = RelationshipType(name=data.name, color=data.color)
    db.add(rt)
    await db.commit()
    await db.refresh(rt)
    return rt


@router.patch("/relationship-types/{rt_id}")
async def update_relationship_type(
    rt_id: uuid.UUID,
    data: RelationshipTypeCreate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rt = await db.get(RelationshipType, rt_id)
    if not rt:
        raise HTTPException(status_code=404, detail="Relationship type not found")
    rt.name = data.name
    rt.color = data.color
    await db.commit()
    await db.refresh(rt)
    return rt


@router.delete("/relationship-types/{rt_id}")
async def delete_relationship_type(
    rt_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rt = await db.get(RelationshipType, rt_id)
    if not rt:
        raise HTTPException(status_code=404, detail="Relationship type not found")
    await db.delete(rt)
    await db.commit()
    return {"status": "deleted"}
