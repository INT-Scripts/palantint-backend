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
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.private.deps import escape_like, require_admin
from db.database import get_db
from db.models import AgendaEvent, Club, RelationshipType, Student, User

router = APIRouter(prefix="/admin", tags=["admin"])


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


@router.get("/telemetry")
async def get_system_telemetry(
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    students_count = await db.scalar(select(func.count(Student.id)))
    clubs_count = await db.scalar(select(func.count(Club.id)))
    events_count = await db.scalar(select(func.count(AgendaEvent.id)))
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
    result = await db.execute(select(Student).where(Student.id == student_id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
        
    student.apartment = data.apartment
    await db.commit()
    return {"status": "success", "apartment": student.apartment}


@router.get("/students/grid")
async def get_students_grid(
    skip: int = 0,
    limit: int = 100,
    search: str = "",
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    query = select(Student, User).outerjoin(User, User.username == Student.trombint_id)
    
    if search:
        s = escape_like(search)
        query = query.where(
            or_(
                Student.first_name.ilike(f"%{s}%"),
                Student.last_name.ilike(f"%{s}%"),
                Student.trombint_id.ilike(f"%{s}%")
            )
        )
        
    query = query.order_by(Student.last_name).offset(skip).limit(limit)
    
    result = await db.execute(query)
    rows = result.all()
    
    return [
        {
            "id": str(student.id),
            "trombint_id": student.trombint_id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "ecole": student.ecole,
            "promo": student.promo,
            "apartment": student.apartment,
            "is_active": student.is_active,
            "user_id": str(user.id) if user else None,
            "is_admin": user.is_admin if user else False
        }
        for student, user in rows
    ]


@router.patch("/students/{student_id}")
async def patch_student_grid(
    student_id: uuid.UUID,
    data: StudentPatch,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Student).where(Student.id == student_id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
        
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(student, key, value)
        
    await db.commit()
    return {"status": "success"}


@router.post("/users/provision")
async def provision_user(
    req: ProvisionRequest,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    from core.auth import get_password_hash
    
    result = await db.execute(select(Student).where(Student.id == req.student_id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
        
    user_check = await db.execute(select(User).where(User.username == student.trombint_id))
    if user_check.scalars().first():
        raise HTTPException(status_code=400, detail="User already provisioned for this student")
        
    alphabet = string.ascii_letters + string.digits
    password = ''.join(secrets.choice(alphabet) for i in range(12))
    
    db_user = User(
        username=student.trombint_id,
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
    query = select(Student).where(
        or_(
            Student.first_name.ilike(f"%{sq}%"),
            Student.last_name.ilike(f"%{sq}%"),
            Student.trombint_id.ilike(f"%{sq}%")
        )
    ).limit(10)
    
    result = await db.execute(query)
    students = result.scalars().all()
    
    return [
        {
            "id": str(s.id),
            "first_name": s.first_name,
            "last_name": s.last_name,
            "trombint_id": s.trombint_id,
            "apartment": s.apartment
        }
        for s in students
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
        select(Student).order_by(Student.last_name, Student.first_name)
    )
    students = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["nom", "prénom", "id utilisateur", "num appart"])

    for s in students:
        writer.writerow(
            [s.last_name or "", s.first_name or "", s.trombint_id or s.id, ""]
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
            select(Student).where(Student.trombint_id == normalized_id)
        )
        student = result.scalars().first()

        if student:
            student.apartment = apartment_num.strip() if apartment_num else None
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
