import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/laundry", tags=["laundry"])

LAUNDRY_URLS = {
    "u3": "https://api.touchnpay.fr/public/406jkjom1ann1k2",
    "u4": "https://api.touchnpay.fr/public/30dwf80glk9eyyff",
    "u5": "https://api.touchnpay.fr/public/30dwf80glk9ezrig",
    "u6": "https://api.touchnpay.fr/public/30dwf80gljgu5fxu",
    "u7": "https://api.touchnpay.fr/public/30dwf80glk9ezzr9",
}

@router.get("/{building}")
async def get_laundry_status(building: str):
    b_key = building.lower()
    if b_key not in LAUNDRY_URLS:
        raise HTTPException(status_code=404, detail="Building laundry not found")
    
    url = LAUNDRY_URLS[b_key]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=15.0)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail="Touch'n'Pay API error")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch laundry data: {str(e)}")
