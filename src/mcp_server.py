import asyncio
import logging
import secrets
from collections import deque
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request

from core.config import settings
from core.rate_limit import get_client_ip, mcp_auth_limiter

logger = logging.getLogger("mcp")

# Configuration from settings
MCP_SERVICE_TOKEN = settings.MCP_SERVICE_TOKEN


class ServiceTokenVerifier(TokenVerifier):
    """Grants access to MCP clients that present MCP_SERVICE_TOKEN as their bearer token.

    This is the same shared secret the MCP server uses to authenticate itself to the
    private API (see api/private/deps.py) — it does not carry per-client identity, it
    simply gates access to the /mcp endpoint itself, which is otherwise unauthenticated.

    Verification attempts are throttled via the same core.rate_limit machinery used by
    every other credential-checking endpoint in the app (login, refresh), keyed by client IP.
    """

    def __init__(self, expected_token: str):
        super().__init__()
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        try:
            mcp_auth_limiter.check(get_client_ip(get_http_request()))
        except RuntimeError:
            # No HTTP request in context (e.g. non-HTTP transport) — nothing to key on.
            pass
        except HTTPException:
            # verify_token runs inside Starlette's AuthenticationMiddleware, outside
            # FastAPI's exception-handling layer, so a raised HTTPException would
            # surface as a raw 500 rather than 429. Deny instead: this reuses the
            # same 401 "invalid_token" response an ordinary bad token gets, so a
            # rate-limited caller learns nothing beyond "not authenticated".
            logger.warning("MCP auth rate limit exceeded, denying token verification.")
            return None

        if not secrets.compare_digest(token, self._expected_token):
            return None
        return AccessToken(token=token, client_id="mcp-service", scopes=[])


if not MCP_SERVICE_TOKEN:
    logger.warning(
        "MCP_SERVICE_TOKEN is not set — the /mcp endpoint will run WITHOUT authentication."
    )

mcp = FastMCP(
    "PalantINT",
    auth=ServiceTokenVerifier(MCP_SERVICE_TOKEN) if MCP_SERVICE_TOKEN else None,
)
mcp._mcp_server.name = "PalantINT"


class PalantINTClient:
    """Thin wrapper around the PalantINT private API, reusing a single
    pooled connection instead of opening a new one per request."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        headers = (
            {"Authorization": f"Bearer {MCP_SERVICE_TOKEN}"}
            if MCP_SERVICE_TOKEN
            else {}
        )
        self._http = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=30.0
        )

    async def aclose(self):
        await self._http.aclose()

    async def _request(self, method: str, endpoint: str, **kwargs):
        url = f"/{endpoint.lstrip('/')}"
        try:
            response = await self._http.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.warning("PalantINT API error on %s %s: %s", method, url, e)
            raise ToolError(
                f"PalantINT API returned an error ({e.response.status_code}) for {endpoint}."
            ) from e
        except httpx.RequestError as e:
            logger.error("PalantINT API unreachable on %s %s: %s", method, url, e)
            raise ToolError(
                "Could not reach the PalantINT API. It may be down or unreachable."
            ) from e

    async def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None):
        return await self._request("GET", endpoint, params=params)

    async def post(self, endpoint: str, json: Optional[Dict[str, Any]] = None):
        return await self._request("POST", endpoint, json=json)

    async def find_student_id(self, trombint_id: str) -> Optional[str]:
        """Convert a TrombintID (string) to a UUID (string)."""
        search_data = await self.get("/search", params={"q": trombint_id})
        target = trombint_id.lower()
        for s in search_data.get("students", []):
            if s["trombint_id"].lower() == target:
                return s["id"]
        return None


client = PalantINTClient("http://localhost:3000/api/private")


@mcp.prompt()
def tool_guide():
    """
    A comprehensive guide on how to use the PalantINT tools effectively.
    """
    return """
# PalantINT Tool Usage Guide

Use these tools to answer questions about campus life, people, clubs, and buildings.

Almost every tool takes a `trombint_id` (a short student identifier), never a name or a UUID
directly. Always resolve names to a `trombint_id` via `search_directory` first — do not guess one.

## People & Directory
1. **Identify / free-text search**: `search_directory(query)` — fuzzy-matches students, clubs, and
   class groups by name, TrombintID, or apartment (e.g. `search_directory(query="jdupont")`).
2. **Browse / filter**: `search_directory(promo=..., ecole=..., building=...)` — list students by
   exact promo, école, or building (e.g. building='7' for U7), optionally combined with `query`.
   Use this instead of `search_directory(query=...)` when you know the criteria but not a name
   (e.g. "who lives in building 7", "list of TSP first-years"). Results are capped by `limit`
   (default 25); a truncation note tells you when to narrow the filters or raise `limit`.
3. **Profile**: `get_student_profile(trombint_id)` — name, promo, school, email, clubs.
4. **Relationships**: `get_student_relationships(trombint_id)` — friends, partners, roommates.
5. **Socials**: `get_student_socials(trombint_id)` — LinkedIn, GitHub, Discord, Instagram, etc.
6. **Notes**: `get_student_notes(trombint_id)` — quotes/notes other students left about them.
7. **Pathfinding**: `find_shortest_path(trombint_id_1, trombint_id_2)` — shortest chain of
   relationships/roommates/shared clubs connecting two students, with degrees of separation.
8. **Relationship taxonomy**: `list_relationship_types()` — the valid relationship categories
   (friend, roommate, partner, etc.) that appear in `get_student_relationships` output.

## Clubs & Academics
- **Club info**: `get_club_info(query)` — description, association, foyer room, links, upcoming
  events. Matches the first club found for `query`; if the query is ambiguous (e.g. an acronym
  matching multiple clubs), mention that to the user and suggest a more specific query.
- **Club members**: `list_club_members(query)` — executive board (mandat) and regular members.
  Same first-match caveat as `get_club_info`.
- **Class roster**: `get_class_roster(query)` — students in a class or promo group
  (e.g. 'TSP_INF1', 'IMT_L3'); matches by substring against the class group name.

## Housing & Location
- **Apartment specs**: `get_apartment_info(apartment_id)` (e.g. '7413') — size, price, floor,
  scholarship allocation.
- **Roommates**: `list_roommates(trombint_id)` — who else lives in the same apartment.
- **Where is student**: `where_is_student(trombint_id, datetime_str)` — infers current location
  (classroom from schedule, or home apartment) at a given ISO datetime, e.g.
  `datetime_str="2026-07-28T14:30:00"`. Omit `datetime_str` to use the current time.
- **Buildings**: `get_map_location(query)` — lists residential buildings and their floors
  (e.g. `query="U3"`). Does not cover individual rooms or classrooms — use the Rooms tools below,
  or `where_is_student`/`get_student_profile` for a specific student's apartment.

## Rooms & Availability
- **List rooms**: `list_rooms()` — every room name that appears in the class schedule, useful to
  disambiguate a room name before using the tools below.
- **Free rooms**: `find_available_rooms(start_time, end_time)` — rooms with no class scheduled
  in a given ISO datetime window, e.g. "is any room free at 3pm today".
- **Room schedule**: `get_room_schedule(room_query, start_date, end_date)` — what's scheduled in
  a room (substring match) over a `YYYY-MM-DD` date range.

## Schedule & Availability
- **Daily schedule**: `get_student_schedule(trombint_id, date)` — classes on a given day; `date`
  is `YYYY-MM-DD` (e.g. `date="2026-07-28"`), defaults to today.
- **Find common time**: `find_common_free_slots(trombint_ids, date)` — common free windows
  (08:00–20:00) across multiple students on one `YYYY-MM-DD` date, for scheduling meetings.

## Campus Status
- **Laundry**: `get_laundry_status()` — real-time washer/dryer availability.

## Tips
- If a tool reports "not found", double-check the `trombint_id` via `search_directory` rather than
  retrying with a guessed ID.
- Dates and datetimes must be ISO formatted (`YYYY-MM-DD` / `YYYY-MM-DDTHH:MM:SS`); anything else
  will be rejected.
"""


@mcp.tool()
async def search_directory(
    query: Optional[str] = None,
    promo: Optional[str] = None,
    ecole: Optional[str] = None,
    building: Optional[str] = None,
    limit: int = 25,
):
    """
    Search or browse the directory for students and clubs. Returns IDs/Slugs for further lookup.

    - **Free-text lookup**: pass only `query` (a name, TrombintID, or apartment, e.g. 'jdupont')
      to fuzzy-search students, clubs, and class groups by relevance.
    - **Filtered browsing**: pass `promo` (e.g. 'Ingénieur 1ère année'), `ecole` (e.g. 'Télécom SudParis'),
      and/or `building` (e.g. '7' for building U7) to list students matching those exact criteria —
      use this for questions like "who's in TSP_INF2" or "students in building 7" instead of guessing
      a search term. `query` can be combined with these filters to narrow further by name.

    `limit` caps how many students are returned (default 25, max 50) to avoid flooding the response.
    """
    limit = max(1, min(limit, 50))

    if promo or ecole or building:
        params: Dict[str, Any] = {"limit": limit}
        if query:
            params["q"] = query
        if promo:
            params["promo"] = promo
        if ecole:
            params["ecole"] = ecole
        if building:
            params["bldg"] = building

        students = await client.get("/students", params=params)
        if not students:
            return f"No students found matching promo={promo!r}, ecole={ecole!r}, building={building!r}, query={query!r}."

        output = [f"### Students Found ({len(students)}{'+' if len(students) == limit else ''})"]
        for s in students:
            output.append(
                f"- {s['first_name']} {s['last_name']} (TrombintID: {s['trombint_id']}, "
                f"Promo: {s.get('promo') or 'N/A'}, School: {s.get('ecole') or 'N/A'}, "
                f"Apt: {s.get('apartment') or 'N/A'})"
            )
        if len(students) == limit:
            output.append("\n_Results were truncated — narrow your filters or raise `limit` for more._")
        return "\n".join(output)

    if not query:
        return "Please provide a `query`, or at least one of `promo`/`ecole`/`building` to browse."

    data = await client.get("/search", params={"q": query})

    output = []
    students = data.get("students", [])[:limit]
    if students:
        output.append(f"### Students Found ({len(students)})")
        for s in students:
            output.append(
                f"- {s['first_name']} {s['last_name']} (TrombintID: {s['trombint_id']}, Apt: {s['apartment'] or 'N/A'})"
            )

    if data.get("clubs"):
        output.append("\n### Clubs Found")
        for c in data["clubs"]:
            output.append(f"- {c['name']} (Slug: {c['slug']})")

    if data.get("class_groups"):
        output.append("\n### Class Groups Found")
        for cg in data["class_groups"]:
            output.append(f"- {cg['name']}")

    return "\n".join(output) if output else f"No results found for '{query}'."


@mcp.tool()
async def get_student_profile(trombint_id: str):
    """
    Get basic profile details: name, promo, school, email, and clubs.
    """
    student_id = await client.find_student_id(trombint_id)
    if not student_id:
        return f"Student '{trombint_id}' not found."

    data = await client.get(f"/students/{student_id}")

    res = [
        f"# {data['first_name']} {data['last_name']}",
        f"- **Promo**: {data.get('promo', 'N/A')}",
        f"- **School**: {data.get('ecole', 'N/A')}",
        f"- **Email**: {data.get('email', 'N/A')}",
        f"- **Apartment**: {data.get('apartment', 'N/A')}",
    ]

    if data.get("clubs"):
        res.append("\n**Clubs**:")
        for sc in data["clubs"]:
            res.append(
                f"- {sc.get('club', {}).get('name')} ({sc.get('role', 'Member')})"
            )

    return "\n".join(res)


@mcp.tool()
async def get_student_socials(trombint_id: str):
    """
    Get registered social media links (LinkedIn, GitHub, Discord, Instagram, etc.) for a student.
    """
    student_id = await client.find_student_id(trombint_id)
    if not student_id:
        return f"Student '{trombint_id}' not found."

    data = await client.get(f"/students/{student_id}")
    socials = data.get("social_links", [])

    if not socials:
        return f"No social links registered for {trombint_id}."

    res = [
        f"# Social Links for {data.get('first_name', '')} {data.get('last_name', '')} ({trombint_id})"
    ]
    for s in socials:
        res.append(f"- **{s['platform']}**: [{s['username']}]({s['url']})")

    return "\n".join(res)


@mcp.tool()
async def get_student_relationships(trombint_id: str):
    """
    Find friends, partners, and roommates of a student.
    """
    student_id = await client.find_student_id(trombint_id)
    if not student_id:
        return f"Student '{trombint_id}' not found."

    data = await client.get(f"/students/{student_id}/relationships")
    if not data:
        return f"No social data found for {trombint_id}."

    res = [f"# Relationships for {trombint_id}"]
    for rel in data:
        other = rel["other_student"]
        rel_type = rel["relationship_type"]["name"]
        res.append(f"- {other['first_name']} {other['last_name']} ({rel_type})")

    return "\n".join(res)


@mcp.tool()
async def get_club_info(query: str):
    """
    Get detailed information about a club, including description, origin association, foyer room, links, and events.
    """
    search_data = await client.get("/search", params={"q": query})
    clubs = search_data.get("clubs", [])
    if not clubs:
        return f"No club found matching '{query}'."

    club_id = clubs[0]["id"]
    data = await client.get(f"/clubs/{club_id}")

    res = [
        f"# {data['name']}",
        f"- **Type**: {data.get('type') or 'Club'}",
        f"- **Association**: {data.get('association_of_origin') or 'N/A'}",
        f"- **Foyer Room**: {data.get('foyer_room') or 'N/A'}",
        f"- **Slug**: {data.get('slug') or 'N/A'}",
    ]
    if data.get("description"):
        res.append(f"\n**Description**:\n{data['description']}")

    if data.get("links"):
        res.append("\n**Links**:")
        for link in data["links"]:
            res.append(f"- [{link['name']}]({link['url']})")

    if data.get("events"):
        res.append("\n**Upcoming Events**:")
        for e in data["events"]:
            start = datetime.fromisoformat(e["start_time"]).strftime("%Y-%m-%d %H:%M")
            res.append(f"- **{e['name']}** ({start}) @ {e.get('room') or 'TBD'}")

    return "\n".join(res)


@mcp.tool()
async def list_club_members(query: str):
    """
    List members and executive board officers (mandats) of a club.
    """
    search_data = await client.get("/search", params={"q": query})
    clubs = search_data.get("clubs", [])
    if not clubs:
        return f"No club found matching '{query}'."

    club_id = clubs[0]["id"]
    data = await client.get(f"/clubs/{club_id}")
    members = data.get("members", [])

    if not members:
        return f"No member roster found for {data['name']}."

    res = [f"# Members of {data['name']} ({len(members)} total)"]

    board = [m for m in members if m.get("is_mandat")]
    regular = [m for m in members if not m.get("is_mandat")]

    if board:
        res.append("\n### Executive Board (Mandat)")
        for m in board:
            res.append(
                f"- **{m['first_name']} {m['last_name']}** ({m.get('role', 'Officer')}) - Promo: {m.get('promo', 'N/A')}"
            )

    if regular:
        res.append("\n### Members")
        for m in regular:
            res.append(
                f"- {m['first_name']} {m['last_name']} ({m.get('role', 'Member')}) - TrombintID: {m.get('trombint_id', 'N/A')}"
            )

    return "\n".join(res)


@mcp.tool()
async def get_class_roster(query: str):
    """
    Get roster of students enrolled in a class or promotion group (e.g. 'TSP_INF1', 'IMT_L3').
    """
    groups = await client.get("/class-groups")
    matched_group = None
    query_lower = query.lower()
    for g in groups:
        if query_lower in g["name"].lower():
            matched_group = g
            break

    if not matched_group:
        return f"No class group found matching '{query}'."

    group_data = await client.get(f"/class-groups/{matched_group['id']}")
    members = group_data.get("members", [])

    if not members:
        return f"No students found in class group '{group_data['name']}'."

    res = [f"# Roster for {group_data['name']} ({len(members)} students)"]
    for m in members:
        res.append(
            f"- **{m['first_name']} {m['last_name']}** (TrombintID: {m.get('trombint_id', 'N/A')}, Promo: {m.get('promo', 'N/A')})"
        )

    return "\n".join(res)


@mcp.tool()
async def get_apartment_info(apartment_id: str):
    """
    Get specifications of a specific apartment (U3-101): size, price, floor.
    """
    all_details = await client.get("/students/apartments/details")
    apt = all_details.get(apartment_id.upper())

    if not apt:
        return f"No technical details found for apartment {apartment_id}."

    return f"""# Apartment {apartment_id}
- **Building**: {apt["Bâtiment"]}
- **Floor**: {apt["Etage"]}
- **Type**: {apt["Type"]}
- **Surface**: {apt["Superficie"]}
- **Price**: {apt["Tarif"]}
- **Allocations**: Boursier: {apt["Allocation boursier"]} / Non-Boursier: {apt["Allocation non boursier"]}"""


@mcp.tool()
async def list_roommates(trombint_id: str):
    """
    List all students living in the same apartment as the given student.
    """
    student_id = await client.find_student_id(trombint_id)
    if not student_id:
        return f"Student '{trombint_id}' not found."

    # Relationships endpoint includes roommates as 'colocataires'
    data = await client.get(f"/students/{student_id}/relationships")
    roommates = [
        rel
        for rel in data
        if rel["relationship_type"]["name"].lower() in ("colocataire", "roommate")
    ]

    if not roommates:
        return f"{trombint_id} has no listed roommates."

    res = [f"# Roommates of {trombint_id}"]
    for r in roommates:
        other = r["other_student"]
        res.append(f"- {other['first_name']} {other['last_name']}")

    return "\n".join(res)


@mcp.tool()
async def get_student_schedule(trombint_id: str, date: Optional[str] = None):
    """
    Get a student's class schedule for a specific date (YYYY-MM-DD). Defaults to today.
    """
    student_id = await client.find_student_id(trombint_id)
    if not student_id:
        return f"Student '{trombint_id}' not found."

    target_date = date or datetime.now().strftime("%Y-%m-%d")

    # We use the compare endpoint for a single student to get their daily agenda
    payload = {
        "student_ids": [student_id],
        "start_date": target_date,
        "end_date": target_date,
    }

    data = await client.post("/agenda/compare", json=payload)
    events = data.get(str(student_id), [])

    if not events:
        return f"No classes found for {trombint_id} on {target_date}."

    res = [f"# Schedule for {trombint_id} on {target_date}"]
    for e in events:
        start = datetime.fromisoformat(e["start_time"]).strftime("%H:%M")
        end = datetime.fromisoformat(e["end_time"]).strftime("%H:%M")
        res.append(f"- {start}-{end}: **{e['name']}** ({e['type']})")

    return "\n".join(res)


@mcp.tool()
async def find_common_free_slots(
    trombint_ids: list[str], date: Optional[str] = None
):
    """
    Find common free time slots between multiple students on a specific date (YYYY-MM-DD). Defaults to today.
    """
    if not trombint_ids:
        return "Please provide at least one student TrombintID."

    target_date = date or datetime.now().strftime("%Y-%m-%d")

    resolved = await asyncio.gather(*(client.find_student_id(tid) for tid in trombint_ids))

    student_ids = []
    found_map = {}
    for tid, sid in zip(trombint_ids, resolved):
        if sid:
            student_ids.append(sid)
            found_map[sid] = tid

    if not student_ids:
        return "None of the specified students were found."

    payload = {
        "student_ids": student_ids,
        "start_date": target_date,
        "end_date": target_date,
    }

    agenda_data = await client.post("/agenda/compare", json=payload)

    # Collect occupied intervals in minutes from 08:00 to 20:00 (480m to 1200m)
    day_start = 8 * 60  # 08:00
    day_end = 20 * 60  # 20:00
    busy_intervals = []

    for sid in student_ids:
        events = agenda_data.get(str(sid), [])
        for e in events:
            st = datetime.fromisoformat(e["start_time"])
            et = datetime.fromisoformat(e["end_time"])
            s_min = max(day_start, st.hour * 60 + st.minute)
            e_min = min(day_end, et.hour * 60 + et.minute)
            if s_min < e_min:
                busy_intervals.append((s_min, e_min))

    # Merge overlapping busy intervals
    busy_intervals.sort(key=lambda x: x[0])
    merged_busy = []
    for interval in busy_intervals:
        if not merged_busy or merged_busy[-1][1] < interval[0]:
            merged_busy.append(interval)
        else:
            merged_busy[-1] = (
                merged_busy[-1][0],
                max(merged_busy[-1][1], interval[1]),
            )

    # Compute free intervals
    free_intervals = []
    curr = day_start
    for b_start, b_end in merged_busy:
        if b_start > curr:
            free_intervals.append((curr, b_start))
        curr = max(curr, b_end)
    if curr < day_end:
        free_intervals.append((curr, day_end))

    names = ", ".join(found_map.values())
    res = [f"# Common Free Slots on {target_date}", f"**Students**: {names}\n"]

    if not free_intervals:
        res.append("No common free slots found between 08:00 and 20:00.")
    else:
        for s_min, e_min in free_intervals:
            sh, sm = divmod(s_min, 60)
            eh, em = divmod(e_min, 60)
            res.append(
                f"- **{sh:02d}:{sm:02d} - {eh:02d}:{em:02d}** ({e_min - s_min} mins)"
            )

    return "\n".join(res)


@mcp.tool()
async def find_shortest_path(trombint_id_1: str, trombint_id_2: str):
    """
    Find the shortest connection path between two students via relationships, roommates, or common clubs.
    """
    sid1 = await client.find_student_id(trombint_id_1)
    sid2 = await client.find_student_id(trombint_id_2)

    if not sid1:
        return f"Student '{trombint_id_1}' not found."
    if not sid2:
        return f"Student '{trombint_id_2}' not found."

    if sid1 == sid2:
        return "Both arguments refer to the same student."

    graph = await client.get("/graph")
    nodes = {n["id"]: n for n in graph.get("nodes", [])}

    # Build adjacency list: node_id -> list of (neighbor_id, edge_label)
    adj = {}
    for link in graph.get("links", []):
        src, tgt, label = (
            link["source"],
            link["target"],
            link.get("label", "connected"),
        )
        adj.setdefault(src, []).append((tgt, label))
        adj.setdefault(tgt, []).append((src, label))

    # BFS from sid1 to sid2
    queue = deque([[sid1]])
    visited = {sid1}
    parent_edge = {}

    found_path = None
    while queue:
        path = queue.popleft()
        curr = path[-1]

        if curr == sid2:
            found_path = path
            break

        for neighbor, label in adj.get(curr, []):
            if neighbor not in visited:
                visited.add(neighbor)
                parent_edge[(curr, neighbor)] = label
                queue.append(path + [neighbor])

    if not found_path:
        return f"No path found between '{trombint_id_1}' and '{trombint_id_2}' in the social graph."

    res = [
        f"# Connection Path: {trombint_id_1} ↔ {trombint_id_2}",
        f"**Degrees of Separation**: {len(found_path) - 1}\n",
    ]
    for i in range(len(found_path) - 1):
        u, v = found_path[i], found_path[i + 1]
        u_node = nodes.get(u, {})
        v_node = nodes.get(v, {})
        u_name = u_node.get("name") or u
        v_name = v_node.get("name") or v
        edge_label = (
            parent_edge.get((u, v))
            or parent_edge.get((v, u))
            or "connected"
        )
        res.append(f"{i + 1}. **{u_name}** --(*{edge_label}*)--> **{v_name}**")

    return "\n".join(res)


@mcp.tool()
async def where_is_student(
    trombint_id: str, datetime_str: Optional[str] = None
):
    """
    Infer a student's location (classroom or apartment) at a given datetime (ISO format YYYY-MM-DDTHH:MM:SS) or current time.
    """
    student_id = await client.find_student_id(trombint_id)
    if not student_id:
        return f"Student '{trombint_id}' not found."

    student_data = await client.get(f"/students/{student_id}")
    apt = student_data.get("apartment") or "Unknown Apartment"
    full_name = f"{student_data.get('first_name', '')} {student_data.get('last_name', '')}"

    if datetime_str:
        try:
            target_dt = datetime.fromisoformat(datetime_str)
        except ValueError:
            return f"Invalid ISO datetime format: '{datetime_str}'."
    else:
        target_dt = datetime.now()

    target_date = target_dt.strftime("%Y-%m-%d")

    # Query schedule for that date
    payload = {
        "student_ids": [student_id],
        "start_date": target_date,
        "end_date": target_date,
    }
    events_data = await client.post("/agenda/compare", json=payload)
    events = events_data.get(str(student_id), [])

    active_event = None
    for e in events:
        st = datetime.fromisoformat(e["start_time"])
        et = datetime.fromisoformat(e["end_time"])
        if st <= target_dt <= et:
            active_event = e
            break

    dt_formatted = target_dt.strftime("%Y-%m-%d %H:%M")
    if active_event:
        room = active_event.get("room") or "Classroom (Unspecified)"
        return (
            f"# Location Inference for {full_name} ({trombint_id})\n"
            f"- **Time**: {dt_formatted}\n"
            f"- **Status**: IN CLASS\n"
            f"- **Class**: {active_event['name']} ({active_event.get('type', 'Course')})\n"
            f"- **Location**: {room}"
        )
    else:
        return (
            f"# Location Inference for {full_name} ({trombint_id})\n"
            f"- **Time**: {dt_formatted}\n"
            f"- **Status**: NO ACTIVE CLASS\n"
            f"- **Inferred Location**: Apartment **{apt}** (Maisel Residence)"
        )


@mcp.tool()
async def get_student_notes(trombint_id: str):
    """
    Get 'Media' entries (quotes, funny notes) about a student.
    """
    student_id = await client.find_student_id(trombint_id)
    if not student_id:
        return f"Student '{trombint_id}' not found."

    data = await client.get(f"/students/{student_id}/media")
    # Only show NOTES/TEXT type media
    notes = [m for m in data if m.get("type") == "NOTE" or m.get("content")]

    if not notes:
        return f"No notes found for {trombint_id}."

    res = [f"# Notes about {trombint_id}"]
    for n in notes:
        author = n.get("author_name") or "Anonymous"
        res.append(f'> "{n["content"]}"\n— *{author}*')

    return "\n".join(res)


@mcp.tool()
async def get_laundry_status():
    """
    Check real-time machine availability.
    """
    data = await client.get("/laundry/status")
    if not data or not data.get("machines"):
        return "Laundry status unavailable."

    res = ["# Laundry Status"]
    for m in data["machines"]:
        status = "FREE" if m["available"] else f"BUSY ({m['time_remaining']}m left)"
        res.append(f"- Machine {m['id']} ({m['type']}): {status}")
    return "\n".join(res)


@mcp.tool()
async def get_map_location(query: Optional[str] = None):
    """
    List campus residential buildings and their floors. Pass `query` (e.g. 'U3') to filter
    to a single building; omit it to list all buildings.

    For a specific student's apartment, use `get_student_profile` or `where_is_student` instead —
    this tool only covers building/floor structure, not individual rooms or occupants.
    """
    data = await client.get("/maps/buildings")
    if not data:
        return "No building data available."

    if query:
        matched = {b: floors for b, floors in data.items() if query.lower() in b.lower()}
        if not matched:
            return f"No building found matching '{query}'."
        data = matched

    res = ["# Buildings"]
    for building, floors in data.items():
        res.append(f"- **{building}**: floors {', '.join(floors)}")
    return "\n".join(res)


@mcp.tool()
async def list_rooms():
    """
    List all distinct room names found in the class schedule (useful to disambiguate a room
    before calling `get_room_schedule` or checking `find_available_rooms`).
    """
    rooms = await client.get("/agenda/rooms/list")
    if not rooms:
        return "No rooms found."
    return "# Rooms\n" + "\n".join(f"- {r}" for r in rooms)


@mcp.tool()
async def find_available_rooms(start_time: str, end_time: str):
    """
    Find rooms with no scheduled class between `start_time` and `end_time`
    (ISO datetimes, e.g. '2026-07-28T14:00:00' / '2026-07-28T16:00:00').
    """
    rooms = await client.get(
        "/agenda/rooms/available", params={"start_time": start_time, "end_time": end_time}
    )
    if not rooms:
        return f"No rooms available between {start_time} and {end_time}."
    return f"# Available Rooms ({start_time} - {end_time})\n" + "\n".join(
        f"- {r}" for r in rooms
    )


@mcp.tool()
async def get_room_schedule(room_query: str, start_date: str, end_date: str):
    """
    Get the class schedule for rooms matching `room_query` (substring match) between
    `start_date` and `end_date` (YYYY-MM-DD, e.g. '2026-07-28').
    """
    events = await client.get(
        "/agenda/rooms/occupancy",
        params={"room_query": room_query, "start_date": start_date, "end_date": end_date},
    )
    if not events:
        return f"No events found for rooms matching '{room_query}' between {start_date} and {end_date}."

    res = [f"# Schedule for rooms matching '{room_query}' ({start_date} - {end_date})"]
    for e in events:
        start = datetime.fromisoformat(e["start_time"]).strftime("%Y-%m-%d %H:%M")
        end = datetime.fromisoformat(e["end_time"]).strftime("%H:%M")
        res.append(f"- {start}-{end} @ **{e['room']}**: {e['name']} ({e['type']})")
    return "\n".join(res)


@mcp.tool()
async def list_relationship_types():
    """
    List the valid relationship categories (e.g. friend, roommate, partner) used by
    `get_student_relationships` and `find_shortest_path`.
    """
    types = await client.get("/relationship-types")
    if not types:
        return "No relationship types found."
    return "# Relationship Types\n" + "\n".join(f"- {t['name']}" for t in types)
