import time
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

# Simple in-memory cache
# Format: { "u3": { "data": [...], "timestamp": 1234567890.0 } }
LAUNDRY_CACHE = {}
CACHE_TTL = 30.0  # seconds

# Standard browser headers to look legitimate and avoid quick rate-limiting
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr,en-US;q=0.7,en;q=0.3",
}

async def fetch_building_status(building: str) -> list:
    """Fetch (cached, TTL=30s) machine status for one building from Touch'n'Pay.
    Shared by the public per-building route and the private aggregate route
    (api/private/laundry.py) so both honor the same cache and fallback logic."""
    b_key = building.lower()
    if b_key not in LAUNDRY_URLS:
        raise HTTPException(status_code=404, detail="Building laundry not found")

    now = time.time()
    cached = LAUNDRY_CACHE.get(b_key)

    # If cached data is fresh enough, return it immediately
    if cached and (now - cached["timestamp"] < CACHE_TTL):
        return cached["data"]

    url = LAUNDRY_URLS[b_key]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=HEADERS, timeout=10.0)
            response.raise_for_status()
            data = response.json()

            # Update cache on success
            LAUNDRY_CACHE[b_key] = {
                "data": data,
                "timestamp": now
            }
            return data

        except Exception as e:
            # Fallback to cached data if remote API fails (e.g. 429 Rate Limit)
            if cached:
                print(f"⚠️ Touch'n'Pay API failed ({str(e)}). Returning stale cache for {b_key}.")
                return cached["data"]

            # If no cache is available, raise appropriate error
            if isinstance(e, httpx.HTTPStatusError):
                raise HTTPException(
                    status_code=e.response.status_code,
                    detail=f"Touch'n'Pay API error: {e.response.reason_phrase}"
                )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch laundry data: {str(e)}"
            )


@router.get("/{building}")
async def get_laundry_status(building: str):
    return await fetch_building_status(building)

