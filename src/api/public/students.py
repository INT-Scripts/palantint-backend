from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from db.database import get_db
from db.models import Location

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/apartments/details")
async def get_apartment_details(
    db: AsyncSession = Depends(get_db)
):
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
