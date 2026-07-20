import os
import shutil
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.private.deps import User, require_admin, require_user
from core.config import settings
from db.database import get_db
from db.models import Media, Student

router = APIRouter(tags=["media"])

# Admin configurable limit, can be loaded from DB or ENV. Set 50MB default.
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
UPLOAD_DIR = settings.MEDIA_DIR

ALLOWED_MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov"}


@router.get("/media/{media_id}/file")
async def serve_media_file(
    media_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Serve the actual media file by media ID."""
    media = await db.get(Media, media_id)
    if not media or not media.file_path:
        raise HTTPException(status_code=404, detail="Media file not found")

    abs_path = os.path.abspath(media.file_path)
    if "/app/assets/media" in abs_path:
        abs_path = abs_path.replace("/app/assets/media", "/app/private_assets/media")
        
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(abs_path)


@router.get("/students/{student_id}/media")
async def get_student_media(
    student_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Get all media for a student, with uploader info."""
    result = await db.execute(
        select(Media)
        .options(selectinload(Media.uploader))
        .where(Media.student_id == student_id)
        .order_by(Media.uploaded_at.desc())
    )
    items = result.scalars().all()
    output = []
    for item in items:
        d = {
            "id": str(item.id),
            "student_id": str(item.student_id),
            "type": item.type,
            "file_path": item.file_path,
            "content": item.content,
            "author_name": item.author_name,
            "uploaded_at": str(item.uploaded_at),
            "uploader_username": item.uploader.username if item.uploader else None,
        }
        output.append(d)
    return output


@router.post("/students/{student_id}/media")
async def upload_media(
    student_id: uuid.UUID,
    type: str = Form(...),  # 'IMAGE', 'VIDEO', 'NOTE'
    author_name: str = Form(None),
    content: str = Form(None),  # Used if type is NOTE
    file: UploadFile = File(None),
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    file_path = None
    if type in ["IMAGE", "VIDEO"]:
        if not file:
            raise HTTPException(
                status_code=400, detail="File is required for IMAGE or VIDEO"
            )

        # Check size limit
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)

        if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                status_code=413, detail=f"File too large. Max {MAX_FILE_SIZE_MB}MB"
            )

        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_MEDIA_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File type '{ext}' not allowed. Accepted: {', '.join(sorted(ALLOWED_MEDIA_EXTENSIONS))}"
            )
        filename = f"{uuid.uuid4()}{ext}"
        file_path = os.path.join(UPLOAD_DIR, filename)

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

    elif type == "NOTE":
        if not content:
            raise HTTPException(status_code=400, detail="Content is required for NOTE")
    else:
        raise HTTPException(status_code=400, detail="Invalid media type")

    media = Media(
        student_id=student_id,
        type=type,
        file_path=file_path,
        content=content,
        author_name=author_name,
        uploaded_by_user_id=current_admin.id,
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    return {
        "id": str(media.id),
        "student_id": str(media.student_id),
        "type": media.type,
        "file_path": media.file_path,
        "content": media.content,
        "author_name": media.author_name,
        "uploaded_at": str(media.uploaded_at),
        "uploader_username": current_admin.username,
    }


@router.delete("/media/{media_id}")
async def delete_media(
    media_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    media = await db.get(Media, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    if media.file_path and os.path.exists(media.file_path):
        try:
            os.remove(media.file_path)
        except OSError:
            pass

    await db.delete(media)
    await db.commit()
    return {"status": "deleted"}


