import asyncio
from typing import Optional

from fastapi import APIRouter, Depends

from api.private.deps import User, require_user
from api.public.laundry import LAUNDRY_URLS, fetch_building_status

router = APIRouter(prefix="/laundry", tags=["laundry"])


@router.get("/status")
async def get_laundry_status(
    building: Optional[str] = None,
    current_user: User = Depends(require_user),
):
    """Aggregate machine status across all (or one) laundry-equipped building.
    Was missing from the private router entirely — mcp_server.py's
    get_laundry_status tool has been calling this path since before this
    schema migration and always 404ing."""
    if building:
        return {building.lower(): await fetch_building_status(building)}

    results = await asyncio.gather(
        *(fetch_building_status(b) for b in LAUNDRY_URLS),
        return_exceptions=True,
    )
    return {
        b: (data if not isinstance(data, Exception) else [])
        for b, data in zip(LAUNDRY_URLS.keys(), results)
    }
