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
)
from db.database import get_db
from db.models import User

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


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
