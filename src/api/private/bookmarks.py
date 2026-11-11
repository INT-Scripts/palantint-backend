"""Places of the campus map bookmarked by the signed-in user."""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.private.deps import require_user
from db.database import get_db
from db.models import PlaceBookmark, User

router = APIRouter(prefix="/maps/indoor/bookmarks", tags=["indoor-maps"])


class BookmarkIn(BaseModel):
    feature_id: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=200)
    building_id: str | None = Field(default=None, max_length=100)
    level: float | None = None
    location_id: uuid.UUID | None = None


def serialize(bookmark):
    return {
        "feature_id": bookmark.feature_id,
        "name": bookmark.name,
        "building_id": bookmark.building_id,
        "level": bookmark.level,
        "location_id": str(bookmark.location_id) if bookmark.location_id else None,
        "created_at": bookmark.created_at.isoformat(),
    }


@router.get("")
async def list_bookmarks(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(PlaceBookmark).where(PlaceBookmark.user_id == user.id).order_by(PlaceBookmark.created_at.desc()))
    ).scalars()
    return {"bookmarks": [serialize(bookmark) for bookmark in rows]}


@router.put("")
async def save_bookmark(data: BookmarkIn, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Adds the place, or refreshes its name, building and level when already bookmarked."""
    bookmark = (
        await db.execute(select(PlaceBookmark).where(PlaceBookmark.user_id == user.id, PlaceBookmark.feature_id == data.feature_id))
    ).scalars().first()
    if bookmark is None:
        bookmark = PlaceBookmark(user_id=user.id, feature_id=data.feature_id, name=data.name)
        db.add(bookmark)
    bookmark.name, bookmark.building_id, bookmark.level, bookmark.location_id = data.name, data.building_id, data.level, data.location_id
    await db.commit()
    await db.refresh(bookmark)
    return serialize(bookmark)


@router.delete("")
async def remove_bookmark(feature_id: str = Query(min_length=1, max_length=300), user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(PlaceBookmark).where(PlaceBookmark.user_id == user.id, PlaceBookmark.feature_id == feature_id))
    await db.commit()
    return {"removed": feature_id}
