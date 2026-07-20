from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.database import get_db

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/apartments/details")
async def get_apartment_details(
    db: AsyncSession = Depends(get_db)
):
    from db.models import ApartmentDetail
    result = await db.execute(select(ApartmentDetail))
    details = result.scalars().all()
    
    out = {}
    for d in details:
        out[d.id] = {
            "Logement": d.id,
            "Bâtiment": d.building,
            "Etage": d.floor,
            "Type": d.type,
            "Superficie": d.surface,
            "Tarif": d.price,
            "Allocation boursier": d.alloc_boursier,
            "Allocation non boursier": d.alloc_non_boursier,
            "_req_b": d.req_b,
            "_req_e": d.req_e
        }
    return out
