"""Public telemetry, with a provider-specific TCP workaround for Docker/VPN MTUs."""

import asyncio
import http.client
import json
import logging
import socket
import time
from collections import defaultdict
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response

router = APIRouter(prefix="/laundry", tags=["laundry"])
logger = logging.getLogger(__name__)
from core.laundry import LAUNDRY_URLS

LAUNDRY_CACHE = {}
CACHE_TTL = 30.0
STALE_TTL = 300.0
_locks = defaultdict(asyncio.Lock)
HEADERS = {"Accept": "application/json", "User-Agent": "PalantINT laundry monitor"}


class LaundryHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Advertise a conservative MSS in the SYN, before the TLS handshake.
        # Without it, Touch'n'Pay's TLS response stalls on the dev Docker/VPN
        # path although ordinary HTTPS sites work. No Docker networks or TLS
        # verification settings need to be changed.
        last_error = None
        for family, kind, protocol, _, address in socket.getaddrinfo(
            self.host, self.port, type=socket.SOCK_STREAM
        ):
            raw = socket.socket(family, kind, protocol)
            try:
                raw.settimeout(self.timeout)
                if hasattr(socket, "TCP_MAXSEG"):
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1200)
                raw.connect(address)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as error:
                raw.close()
                last_error = error
        if last_error:
            raise last_error
        raise OSError("No provider address available")


def _request_provider(url: str) -> list:
    parsed = urlsplit(url)
    connection = LaundryHTTPSConnection(parsed.hostname, timeout=8)
    try:
        connection.request("GET", parsed.path, headers=HEADERS)
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"Provider HTTP {response.status}")
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("Provider response too large")
        data = json.loads(body)
        if not isinstance(data, list) or any(
            not isinstance(item, dict) for item in data
        ):
            raise ValueError("Invalid provider telemetry")
        return data
    finally:
        connection.close()


async def fetch_building_status(building: str) -> list:
    key = building.strip().lower()
    if key not in LAUNDRY_URLS:
        raise HTTPException(status_code=404, detail="Laverie introuvable")
    async with _locks[key]:
        cached = LAUNDRY_CACHE.get(key)
        if cached and time.time() - cached["timestamp"] < CACHE_TTL:
            return cached["data"]
        try:
            # Blocking socket/TLS work stays outside FastAPI's event loop.
            data = await asyncio.to_thread(_request_provider, LAUNDRY_URLS[key])
            LAUNDRY_CACHE[key] = {"data": data, "timestamp": time.time()}
            return data
        except (OSError, ValueError, http.client.HTTPException) as error:
            logger.warning("Laundry %s unavailable: %r", key, error)
            if cached and time.time() - cached["timestamp"] < STALE_TTL:
                return cached["data"]
            raise HTTPException(
                status_code=503,
                detail="Le service de laverie ne répond pas. Réessayez dans quelques instants.",
                headers={"Retry-After": "30"},
            ) from error


@router.get("/{building}")
async def get_laundry_status(building: str, response: Response):
    data = await fetch_building_status(building)
    cached = LAUNDRY_CACHE.get(building.strip().lower())
    if cached:
        age = max(0, int(time.time() - cached["timestamp"]))
        response.headers["X-Laundry-Age"] = str(age)
        response.headers["X-Laundry-Stale"] = str(age >= CACHE_TTL).lower()
    response.headers["Cache-Control"] = "no-store"
    return data
