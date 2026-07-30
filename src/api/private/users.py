from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.private.deps import require_user
from core.auth import decrypt_secret, encrypt_secret
from db.database import get_db
from db.models import DataSource, ExternalIdentity, Person, User, UserCredential

from pydantic import BaseModel

router = APIRouter(prefix="/users", tags=["users"])

CAS_PROVIDER = "CAS"


class CasCredentialsSchema(BaseModel):
    cas_username: str
    cas_password: str


@router.get("/me/student")
async def get_my_student_profile(
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Person)
        .join(ExternalIdentity, ExternalIdentity.person_id == Person.id)
        .join(DataSource, DataSource.id == ExternalIdentity.source_id)
        .where(
            DataSource.code == "trombint",
            ExternalIdentity.external_id == current_user.username,
        )
    )
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found for this user")
    return student


@router.get("/me/cas-credentials")
async def get_my_cas_credentials(
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(UserCredential).where(
            UserCredential.user_id == current_user.id,
            UserCredential.provider == CAS_PROVIDER,
        )
    )
    credential = result.scalars().first()
    return {
        "has_credentials": credential is not None,
        "cas_username": decrypt_secret(credential.encrypted_username) if credential else "",
    }


@router.post("/me/cas-credentials")
async def save_my_cas_credentials(
    credentials: CasCredentialsSchema,
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(UserCredential).where(
            UserCredential.user_id == current_user.id,
            UserCredential.provider == CAS_PROVIDER,
        )
    )
    credential = result.scalars().first()

    encrypted_username = encrypt_secret(credentials.cas_username)
    encrypted_password = encrypt_secret(credentials.cas_password)

    if credential:
        credential.encrypted_username = encrypted_username
        credential.encrypted_password = encrypted_password
    else:
        credential = UserCredential(
            user_id=current_user.id,
            provider=CAS_PROVIDER,
            encrypted_username=encrypted_username,
            encrypted_password=encrypted_password,
        )
        db.add(credential)

    await db.commit()
    return {"status": "success"}


@router.get("/me")
async def read_users_me(current_user: User = Depends(require_user)):
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "is_admin": current_user.is_admin,
    }
