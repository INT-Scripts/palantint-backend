import uuid
from datetime import timedelta

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
    TokenData,
    create_access_token,
    get_password_hash,
    verify_password,
    encrypt_password,
    decrypt_password,
)
from db.database import get_db
from db.models import User

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


async def get_current_user_optional(
    token: str | None = Depends(oauth2_scheme_optional), db: AsyncSession = Depends(get_db)
) -> User | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
    except JWTError:
        return None

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    return user


async def get_current_user(

    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(
            username=username, is_admin=payload.get("is_admin", False)
        )
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.username == token_data.username))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
    return user


async def get_current_admin_user(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403, detail="The user doesn't have enough privileges"
        )
    return current_user


from pydantic import BaseModel


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False

from db.models import Student, Club, AgendaEvent
from sqlalchemy import func
import secrets
import string

@router.get("/admin/telemetry")
async def get_system_telemetry(
    current_admin: User = Depends(get_current_admin_user),
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

class ApartmentUpdate(BaseModel):
    apartment: str | None

@router.patch("/admin/students/{student_id}/apartment")
async def update_student_apartment(
    student_id: uuid.UUID,
    data: ApartmentUpdate,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Student).where(Student.id == student_id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
        
    student.apartment = data.apartment
    await db.commit()
    return {"status": "success", "apartment": student.apartment}

class StudentPatch(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    ecole: str | None = None
    promo: str | None = None
    apartment: str | None = None

@router.get("/admin/students/grid")
async def get_students_grid(
    skip: int = 0,
    limit: int = 100,
    search: str = "",
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy.orm import aliased
    from sqlalchemy import select, or_, outerjoin
    
    # We want to join User to see who has an account
    query = select(Student, User).outerjoin(User, User.username == Student.trombint_id)
    
    if search:
        query = query.where(
            or_(
                Student.first_name.ilike(f"%{search}%"),
                Student.last_name.ilike(f"%{search}%"),
                Student.trombint_id.ilike(f"%{search}%")
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

@router.patch("/admin/students/{student_id}")
async def patch_student_grid(
    student_id: uuid.UUID,
    data: StudentPatch,
    current_admin: User = Depends(get_current_admin_user),
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

class ProvisionRequest(BaseModel):
    student_id: uuid.UUID
    is_admin: bool = False

@router.post("/admin/users/provision")
async def provision_user(
    req: ProvisionRequest,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
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
    
    return {
        "status": "success",
        "user_id": str(db_user.id),
        "username": db_user.username,
        "generated_password": password,
        "is_admin": db_user.is_admin
    }

from sqlalchemy import or_

@router.get("/admin/students/search")
async def search_students_admin(
    q: str = "",
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    if not q or len(q) < 2:
        return []
    
    query = select(Student).where(
        or_(
            Student.first_name.ilike(f"%{q}%"),
            Student.last_name.ilike(f"%{q}%"),
            Student.trombint_id.ilike(f"%{q}%")
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

@router.post("/auth/login")
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalars().first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "is_admin": user.is_admin},
        expires_delta=access_token_expires,
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/admin/users", response_model=dict)
async def create_user(
    user: UserCreate,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
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


@router.get("/admin/users")
async def get_all_users(
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User))
    users = result.scalars().all()
    return [
        {"id": str(u.id), "username": u.username, "is_admin": u.is_admin} for u in users
    ]


class UserUpdate(BaseModel):
    is_admin: bool


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_admin = data.is_admin
    await db.commit()
    return {"id": str(user.id), "username": user.username, "is_admin": user.is_admin}


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: uuid.UUID,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.delete(user)
    await db.commit()
    return {"status": "success", "message": "User deleted"}


class CasCredentialsUpdate(BaseModel):
    cas_username: str
    cas_password: str

@router.post("/users/me/cas-credentials")
async def update_cas_credentials(
    data: CasCredentialsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    current_user.cas_username = data.cas_username
    current_user.encrypted_cas_password = encrypt_password(data.cas_password)
    await db.commit()
    return {"status": "success", "message": "CAS credentials securely stored"}

@router.get("/users/me/cas-credentials")
async def get_cas_status(current_user: User = Depends(get_current_user)):
    return {
        "has_credentials": current_user.cas_username is not None,
        "cas_username": current_user.cas_username
    }

@router.get("/users/me/student")
async def get_my_student_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Student).where(Student.trombint_id == current_user.username))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found for this user")
    return student

@router.get("/users/me")
async def read_users_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "is_admin": current_user.is_admin,
    }





import csv
import io

from db.models import Student


@router.get("/admin/apartments/template")
async def download_apartment_template(
    current_admin: User = Depends(get_current_admin_user),
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


@router.post("/admin/apartments/upload")
async def upload_apartments_csv(
    file: UploadFile = File(...),
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Must be a CSV file")

    content = await file.read()
    try:
        decoded_content = content.decode("utf-8-sig")
    except Exception:
        decoded_content = content.decode("latin-1")

    # Detect delimiter based on first line
    first_line = decoded_content.split("\n")[0] if decoded_content else ""
    delimiter = ";" if ";" in first_line else ","

    reader = csv.DictReader(io.StringIO(decoded_content), delimiter=delimiter)
    # the frontend template provides 'id utilisateur', 'numero appart'
    # handle variations if they rename it or use english

    updated_count = 0
    not_found = []

    for row in reader:
        # try to find columns
        user_id = row.get("id utilisateur") or row.get("trombint_id") or row.get("id")
        apartment_num = (
            row.get("num appart") or row.get("numero appart") or row.get("apartment")
        )

        if not user_id:
            continue

        # normalize user id (e.g strip @)
        normalized_id = user_id.strip()
        if normalized_id.startswith("@"):
            normalized_id = normalized_id[1:]

        # find student
        result = await db.execute(
            select(Student).where(Student.trombint_id == normalized_id)
        )
        student = result.scalars().first()

        if student:
            # set or clear apartment
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


@router.get("/pay5vend/download")
async def download_pay5vend_apk(
    current_user: User = Depends(get_current_user)
):
    import os
    from fastapi.responses import FileResponse
    
    apk_path = "/app/private_assets/pay5vend.apk"
    if not os.path.exists(apk_path):
        raise HTTPException(
            status_code=404,
            detail="Exploit payload not found. Contact administrator."
        )
    return FileResponse(
        path=apk_path,
        media_type="application/vnd.android.package-archive",
        filename="pay5vend.apk"
    )
