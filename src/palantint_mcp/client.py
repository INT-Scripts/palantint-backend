import logging
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastmcp.exceptions import ToolError

from core.config import settings

from .format import call, who

logger = logging.getLogger("mcp")

# How long reference data stays cached. A follow-up call about the same student or
# the same graph within a conversation then skips the loopback round-trip.
RESOLVE_TTL = 600
GRAPH_TTL = 60
GROUPS_TTL = 300
APARTMENTS_TTL = 3600


def _plain(text: str) -> str:
    """Lowercase, accent-free, single-spaced — for comparing typed names."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


def _api_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except Exception:
        return ""
    return detail if isinstance(detail, str) else ""


class PalantINTClient:
    """Thin wrapper around the PalantINT private API, reusing a single
    pooled connection instead of opening a new one per request."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        token = settings.MCP_SERVICE_TOKEN
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30.0)
        # endpoint or resolved reference -> (expires_at, value)
        self._cache: Dict[str, Tuple[float, Any]] = {}

    async def aclose(self):
        await self._http.aclose()

    async def _request(self, method: str, endpoint: str, **kwargs):
        url = f"/{endpoint.lstrip('/')}"
        try:
            response = await self._http.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.warning("PalantINT API error on %s %s: %s", method, url, e)
            # Phrased for the model: each case says whether retrying can help.
            if status in (400, 422):
                detail = _api_detail(e.response) or "invalid arguments"
                message = f"PalantINT rejected the request: {detail}. Fix the arguments before retrying."
            elif status == 404:
                message = "PalantINT found nothing at that reference. Check the arguments; do not retry them unchanged."
            elif status in (401, 403):
                message = "PalantINT refused the MCP service credentials. Do not retry; tell the user the integration is misconfigured."
            else:
                message = f"PalantINT failed ({status}). Do not retry this turn; tell the user the data is temporarily unavailable."
            raise ToolError(message) from e
        except httpx.RequestError as e:
            logger.error("PalantINT API unreachable on %s %s: %s", method, url, e)
            raise ToolError(
                "PalantINT is unreachable. Do not retry this turn; tell the user it is temporarily offline."
            ) from e

    async def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None):
        return await self._request("GET", endpoint, params=params)

    async def post(self, endpoint: str, json: Optional[Dict[str, Any]] = None):
        return await self._request("POST", endpoint, json=json)

    def _cached(self, key: str) -> Any:
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        self._cache.pop(key, None)
        return None

    def _store(self, key: str, value: Any, ttl: float) -> Any:
        self._cache[key] = (time.monotonic() + ttl, value)
        return value

    async def cached_get(self, endpoint: str, ttl: float) -> Any:
        """GET a parameterless reference endpoint, cached for `ttl` seconds."""
        hit = self._cached(endpoint)
        if hit is not None:
            return hit
        return self._store(endpoint, await self.get(endpoint), ttl)

    async def resolve_student(self, ref: str) -> Tuple[Optional[str], str]:
        """Resolve a TrombintID *or* a person's name to (student_uuid, display_name).

        Accepting names as well as IDs removes the mandatory `search_directory` turn
        that small models routinely skip (and then hallucinate an ID for). An exact
        TrombintID always wins; a name that matches exactly one student is taken as
        that student; anything more ambiguous comes back unresolved.

        On failure returns `(None, message)` where the message is already phrased for
        the model — callers just return it verbatim. These are answers, not errors:
        a client counting tool failures should not disable the tool over a typo.
        """
        key = (ref or "").strip().lower()
        if not key:
            return None, "No student given."
        hit = self._cached(f"student:{key}")
        if hit is not None:
            return hit

        students = (await self.get("/search", params={"q": ref})).get("students", [])
        by_id = [s for s in students if (s.get("trombint_id") or "").lower() == key]
        # Not every student has a TrombintID, so an exact full name (either order,
        # accents and case ignored) must be enough to pick one of several matches.
        # Homonyms stay ambiguous.
        wanted = _plain(ref)
        exact = by_id or [
            s for s in students
            if wanted in (_plain(f"{s['first_name']} {s['last_name']}"), _plain(f"{s['last_name']} {s['first_name']}"))
        ]
        if by_id or len(exact) == 1:
            picked = exact[0]
        else:
            picked = students[0] if len(students) == 1 else None

        if picked is None:
            if not students:
                return None, (
                    f"No student matches '{ref}'. Check the spelling, or browse with "
                    f"{call('search_directory', query=ref.split()[-1])}."
                )
            options = "; ".join(who(s) for s in (exact or students)[:8])
            more = f" (+{len(students) - 8} more)" if not exact and len(students) > 8 else ""
            return None, (
                f"'{ref}' matches several students: {options}{more}. "
                "Retry with one TrombintID or exact full name."
            )

        resolved = (picked["id"], f"{picked['first_name']} {picked['last_name']}")
        return self._store(f"student:{key}", resolved, RESOLVE_TTL)

    async def resolve_club(self, ref: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Resolve a club name or slug to (club_payload, note).

        Unlike students, a club query that matches several clubs still picks the best
        match — but `note` then names the runners-up so the model can correct itself
        without spending a turn on a disambiguation round-trip.
        """
        clubs = (await self.get("/search", params={"q": ref})).get("clubs", [])
        if not clubs:
            return None, f"No club matches '{ref}'. Try a shorter name, or {call('search_directory', query=ref)}."
        needle = ref.strip().lower()
        best = next(
            (c for c in clubs if needle in ((c.get("slug") or "").lower(), c["name"].lower())),
            clubs[0],
        )
        others = [c["name"] for c in clubs if c is not best][:3]
        note = "also matched: " + ", ".join(others) if others else ""
        return await self.get(f"/clubs/{best['id']}"), note

    async def resolve_group(self, ref: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Resolve a class group name to (group, note): exact name, then prefix, then
        substring — shortest name first, so 'INF1' never silently becomes 'INF10'."""
        groups: List[Dict[str, Any]] = await self.cached_get("/class-groups", GROUPS_TTL)
        needle = (ref or "").strip().lower()
        if not needle:
            return None, "No class group given."
        for test in (
            lambda n: n == needle,
            lambda n: n.startswith(needle),
            lambda n: needle in n,
        ):
            matches = sorted((g for g in groups if test(g["name"].lower())), key=lambda g: (len(g["name"]), g["name"]))
            if matches:
                others = [g["name"] for g in matches[1:4]]
                return matches[0], ("also matched: " + ", ".join(others) if others else "")
        return None, (
            f"No class group matches '{ref}'. Find the exact name with "
            f"{call('search_directory', query=ref)}."
        )


client = PalantINTClient(settings.MCP_API_BASE_URL)
