import asyncio
import logging
import secrets
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

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

# Default cap on how many rows any tool will emit. The tool surface is designed to
# fit small models: descriptions are one or two lines, every list is capped, and
# responses are plain `key: value` text rather than Markdown, because bold markers
# and headings cost tokens without telling the model anything.
MAX_ROWS = 40


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


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def _fields(*pairs: Tuple[str, Any]) -> str:
    """Join `key: value` pairs on one line, dropping the ones with no value.

    Omitting a field entirely is cheaper than printing 'N/A' and reads the same.
    """
    return ", ".join(f"{k}: {v}" for k, v in pairs if v)


def _block(title: str, rows: List[str], limit: int = MAX_ROWS) -> str:
    """Render `title (count):` followed by one row per line, capped at `limit`.

    The truncation note is what tells the model to narrow its query rather than
    silently reasoning over a partial list.
    """
    if not rows:
        return f"{title}: none"
    body = "\n".join(rows[:limit])
    if len(rows) > limit:
        body += f"\n(+{len(rows) - limit} more — narrow the query)"
    return f"{title} ({len(rows)}):\n{body}"


def _who(person: Dict[str, Any]) -> str:
    """'trombintid Firstname Lastname', falling back to the name alone."""
    name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
    tid = person.get("trombint_id")
    return f"{tid} {name}" if tid else name


def _hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #

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
        # Resolved student references, keyed by the lowercased reference the caller
        # used. Lets a follow-up tool call on the same person skip the search round-trip.
        self._resolved: Dict[str, Tuple[str, str]] = {}

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

    async def resolve_student(self, ref: str) -> Tuple[Optional[str], str]:
        """Resolve a TrombintID *or* a person's name to (student_uuid, display_name).

        Accepting names as well as IDs removes the mandatory `search_directory` turn
        that small models routinely skip (and then hallucinate an ID for). An exact
        TrombintID always wins; a name that matches exactly one student is taken as
        that student; anything more ambiguous comes back unresolved.

        On failure returns `(None, message)` where the message is already phrased for
        the model — callers just return it verbatim.
        """
        key = (ref or "").strip().lower()
        if not key:
            return None, "No student given."
        if key in self._resolved:
            return self._resolved[key]

        students = (await self.get("/search", params={"q": ref})).get("students", [])
        exact = [s for s in students if (s.get("trombint_id") or "").lower() == key]
        picked = exact[0] if exact else (students[0] if len(students) == 1 else None)

        if picked is None:
            if not students:
                return None, f"No student matches '{ref}'."
            options = "; ".join(_who(s) for s in students[:8])
            return None, f"'{ref}' matches several students: {options}. Retry with one TrombintID."

        resolved = (picked["id"], f"{picked['first_name']} {picked['last_name']}")
        if len(self._resolved) > 512:
            self._resolved.clear()
        self._resolved[key] = resolved
        return resolved

    async def resolve_club(self, ref: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Resolve a club name or slug to (club_payload, note).

        Unlike students, a club query that matches several clubs still picks the best
        match — but `note` then names the runners-up so the model can correct itself
        without spending a turn on a disambiguation round-trip.
        """
        clubs = (await self.get("/search", params={"q": ref})).get("clubs", [])
        if not clubs:
            return None, f"No club matches '{ref}'."
        note = ""
        if len(clubs) > 1:
            note = "also matched: " + ", ".join(c["name"] for c in clubs[1:4])
        return await self.get(f"/clubs/{clubs[0]['id']}"), note


client = PalantINTClient("http://localhost:3000/api/private")


@mcp.prompt()
def tool_guide():
    """How to use the PalantINT tools."""
    return """PalantINT answers questions about campus people, clubs, rooms and housing.

Rules:
- Student tools accept a TrombintID or a full name directly. Do not call search_directory
  first just to get an ID, and never invent one. If a name is ambiguous the tool replies
  with the matching TrombintIDs — retry with one of them.
- Use search_directory when you need to browse (promo, school, building) or to find a club
  or class group, not to resolve a name you already have.
- Dates are YYYY-MM-DD, datetimes are YYYY-MM-DDTHH:MM:SS. Anything else is rejected.
- Lists are capped. A "(+N more)" line means narrow the query, not that the data is missing.
"""


# --------------------------------------------------------------------------- #
# Directory
# --------------------------------------------------------------------------- #

@mcp.tool()
async def search_directory(
    query: str = "",
    promo: str = "",
    ecole: str = "",
    building: str = "",
    limit: int = 25,
):
    """Search students, clubs and class groups, or browse students by filter.

    query: name, TrombintID or apartment. promo/ecole/building: exact filters for
    browsing ('7' means building U7); combine with query to narrow further.
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
            filters = _fields(
                ("promo", promo), ("ecole", ecole), ("building", building), ("query", query)
            )
            return f"No students match {filters}."

        # Columns pinned by a filter are identical on every row, so print them once
        # in the header instead of repeating them 25 times.
        pinned = _fields(("promo", promo), ("school", ecole), ("building", building))
        rows = [
            ", ".join(
                [_who(s)]
                + [
                    f"{k}: {v}"
                    for k, v in (
                        ("promo", None if promo else s.get("promo")),
                        ("school", None if ecole else s.get("ecole")),
                        ("apt", s.get("apartment")),
                    )
                    if v
                ]
            )
            for s in students
        ]
        out = _block(f"students {pinned}" if pinned else "students", rows, limit)
        if len(students) == limit:
            out += "\n(limit reached — narrow the filters or raise limit)"
        return out

    if not query:
        return "Give a query, or one of promo/ecole/building to browse."

    data = await client.get("/search", params={"q": query})
    out = []

    students = data.get("students", [])[:limit]
    if students:
        out.append(
            _block(
                "students",
                [f"{_who(s)}, apt: {s['apartment']}" if s.get("apartment") else _who(s) for s in students],
                limit,
            )
        )
    if data.get("clubs"):
        out.append("clubs: " + "; ".join(f"{c['name']} ({c['slug']})" for c in data["clubs"]))
    if data.get("class_groups"):
        out.append("groups: " + "; ".join(cg["name"] for cg in data["class_groups"]))

    return "\n".join(out) if out else f"No results for '{query}'."


@mcp.tool()
async def get_student(
    student: str,
    info: Literal["profile", "socials", "relations", "notes", "all"] = "profile",
):
    """Look up one student by TrombintID or full name.

    info: profile (promo, school, email, apartment, clubs) | socials | relations
    (friends, partners, roommates) | notes (quotes left by others) | all.
    """
    student_id, who = await client.resolve_student(student)
    if not student_id:
        return who

    # /students/{id} already carries socials, clubs, groups and notes, so only the
    # relations view costs a second request.
    want_rels = info in ("relations", "all")
    fetches = [client.get(f"/students/{student_id}")]
    if want_rels:
        fetches.append(client.get(f"/students/{student_id}/relationships"))
    results = await asyncio.gather(*fetches)
    data, rels = results[0], (results[1] if want_rels else [])

    tid = data.get("trombint_id") or student
    out = [f"{who} ({tid})"]

    if info in ("profile", "all"):
        out.append(
            _fields(
                ("promo", data.get("promo")),
                ("school", data.get("ecole")),
                ("email", data.get("email")),
                ("apt", data.get("apartment")),
            )
        )
        if data.get("clubs"):
            out.append(
                "clubs: "
                + "; ".join(
                    f"{c.get('club_name')} ({c.get('role') or 'member'})" for c in data["clubs"]
                )
            )
        if data.get("class_groups"):
            out.append(
                "groups: " + "; ".join(g.get("class_group_name") for g in data["class_groups"])
            )

    if info in ("socials", "all"):
        socials = data.get("social_links") or []
        out.append(
            "socials: " + "; ".join(f"{s['platform']} {s['url']}" for s in socials)
            if socials
            else "socials: none"
        )

    if want_rels:
        # Group by relationship type: one line per type beats one line per person.
        by_type: Dict[str, List[str]] = {}
        for rel in rels or []:
            by_type.setdefault(rel["relationship_type"]["name"].lower(), []).append(
                _who(rel["other_student"])
            )
        out.append(
            _block("relations", [f"{t}: {'; '.join(p)}" for t, p in by_type.items()])
            if by_type
            else "relations: none"
        )

    if info in ("notes", "all"):
        notes = [m for m in (data.get("media") or []) if m.get("content")]
        out.append(
            _block("notes", [f'"{n["content"]}" — {n.get("author_name") or "anonymous"}' for n in notes])
            if notes
            else "notes: none"
        )

    return "\n".join(line for line in out if line)


@mcp.tool()
async def find_shortest_path(student_a: str, student_b: str):
    """Shortest chain of relationships, roommates or shared clubs linking two students."""
    (sid1, who1), (sid2, who2) = await asyncio.gather(
        client.resolve_student(student_a), client.resolve_student(student_b)
    )
    if not sid1:
        return who1
    if not sid2:
        return who2
    if sid1 == sid2:
        return "Both refer to the same student."

    graph = await client.get("/graph")
    nodes = {n["id"]: n for n in graph.get("nodes", [])}

    adj: Dict[str, List[Tuple[str, str]]] = {}
    for link in graph.get("links", []):
        src, tgt, label = link["source"], link["target"], link.get("label", "linked")
        adj.setdefault(src, []).append((tgt, label))
        adj.setdefault(tgt, []).append((src, label))

    queue = deque([[sid1]])
    visited = {sid1}
    parent_edge: Dict[Tuple[str, str], str] = {}

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
        return f"No path between {who1} and {who2}."

    # One line for the whole chain: "A -(friend)- B -(club BDE)- C".
    chain = nodes.get(found_path[0], {}).get("name") or found_path[0]
    for u, v in zip(found_path, found_path[1:]):
        label = parent_edge.get((u, v)) or parent_edge.get((v, u)) or "linked"
        chain += f" -({label})- " + (nodes.get(v, {}).get("name") or v)

    return f"{len(found_path) - 1} degrees\n{chain}"


# --------------------------------------------------------------------------- #
# Clubs & academics
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_club(club: str, info: Literal["info", "members"] = "info"):
    """Club details, or its member list. club: name or slug.

    info: info (description, association, foyer room, links, events) | members.
    """
    data, note = await client.resolve_club(club)
    if not data:
        return note

    out = [f"{data['name']} ({data.get('slug') or '?'})"]
    if note:
        out.append(note)

    if info == "info":
        out.append(
            _fields(
                ("type", data.get("type") or "club"),
                ("association", data.get("association_of_origin")),
                ("foyer", data.get("foyer_room")),
            )
        )
        if data.get("description"):
            out.append(data["description"])
        if data.get("links"):
            out.append(
                "links: " + "; ".join(f"{ln['name']} {ln['url']}" for ln in data["links"])
            )
        if data.get("events"):
            out.append(
                _block(
                    "events",
                    [
                        f"{datetime.fromisoformat(e['start_time']):%Y-%m-%d %H:%M} {e['name']}"
                        + (f" @ {e['room']}" if e.get("room") else "")
                        for e in data["events"]
                    ],
                    10,
                )
            )
        return "\n".join(line for line in out if line)

    members = data.get("members") or []
    if not members:
        return f"{data['name']}: no member roster."

    board = [m for m in members if m.get("is_mandat")]
    regular = [m for m in members if not m.get("is_mandat")]
    if board:
        out.append(
            "board: " + "; ".join(f"{_who(m)} ({m.get('role') or 'officer'})" for m in board)
        )
    if regular:
        out.append(_block("members", [_who(m) for m in regular]))
    return "\n".join(line for line in out if line)


@mcp.tool()
async def get_class_roster(group: str):
    """List students in a class or promotion group, e.g. 'TSP_INF1' or 'IMT_L3'."""
    groups = await client.get("/class-groups")
    needle = group.lower()
    matched = next((g for g in groups if needle in g["name"].lower()), None)
    if not matched:
        return f"No class group matches '{group}'."

    data = await client.get(f"/class-groups/{matched['id']}")
    members = data.get("members") or []
    if not members:
        return f"{data['name']}: no students."

    return _block(data["name"], [_who(m) for m in members], 60)


# --------------------------------------------------------------------------- #
# Housing & campus
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_apartment_info(apartment: str):
    """Specs of a Maisel apartment by its code, e.g. '7413': floor, type, size, price, aid."""
    details = await client.get("/students/apartments/details")
    apt = details.get(apartment.upper())
    if not apt:
        return f"No details for apartment {apartment}."

    return f"apt {apartment}: " + _fields(
        ("building", apt.get("Bâtiment")),
        ("floor", apt.get("Etage")),
        ("type", apt.get("Type")),
        ("size", apt.get("Superficie")),
        ("price", apt.get("Tarif")),
        ("aid scholarship", apt.get("Allocation boursier")),
        ("aid standard", apt.get("Allocation non boursier")),
    )


@mcp.tool()
async def get_laundry_status(building: str = ""):
    """Washer/dryer availability. building e.g. 'U3'; omit it for every building."""
    data = await client.get(
        "/laundry/status", params={"building": building} if building else None
    )
    if not data:
        return "Laundry status unavailable."

    out = []
    for bldg, machines in data.items():
        if not machines:
            continue
        # Two lines per building listing machine numbers, rather than one line per machine.
        dryers = [m for m in machines if m.get("machine_type") == "sl"]
        washers = [m for m in machines if m.get("machine_type") != "sl"]
        for kind, group in (("washers", washers), ("dryers", dryers)):
            if not group:
                continue
            free = [str(m.get("machine_nbr")) for m in group if not m.get("started_at")]
            busy = [str(m.get("machine_nbr")) for m in group if m.get("started_at")]
            out.append(
                f"{bldg.upper()} {kind}: "
                + _fields(("free", ",".join(free)), ("busy", ",".join(busy)))
            )

    return "\n".join(out) if out else "Laundry status unavailable."


@mcp.tool()
async def list_reference(kind: Literal["rooms", "buildings", "relationship_types"]):
    """List fixed reference values.

    rooms: room names usable with the room tools. buildings: residences and their floors.
    relationship_types: the categories used by get_student(info='relations').
    """
    if kind == "rooms":
        rooms = await client.get("/agenda/rooms/list")
        return _block("rooms", sorted(rooms), 80) if rooms else "rooms: none"

    if kind == "buildings":
        data = await client.get("/maps/buildings")
        if not data:
            return "buildings: none"
        return "\n".join(f"{b}: floors {', '.join(floors)}" for b, floors in data.items())

    types = await client.get("/relationship-types")
    if not types:
        return "relationship types: none"
    return "relationship types: " + "; ".join(t["name"] for t in types)


# --------------------------------------------------------------------------- #
# Schedules & rooms
# --------------------------------------------------------------------------- #

async def _agenda(student_ids: List[str], date: str) -> Dict[str, Any]:
    return await client.post(
        "/agenda/compare",
        json={"student_ids": student_ids, "start_date": date, "end_date": date},
    )


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


@mcp.tool()
async def get_student_schedule(student: str, date: str = ""):
    """A student's classes on one date. date: YYYY-MM-DD, default today."""
    student_id, who = await client.resolve_student(student)
    if not student_id:
        return who

    day = date or _today()
    events = (await _agenda([student_id], day)).get(str(student_id), [])
    if not events:
        return f"{who} has no class on {day}."

    return _block(
        f"{who} {day}",
        [
            f"{_hhmm(e['start_time'])}-{_hhmm(e['end_time'])} {e['name']} ({e['type']})"
            + (f" @ {e['room']}" if e.get("room") else "")
            for e in events
        ],
    )


@mcp.tool()
async def find_common_free_slots(students: List[str], date: str = ""):
    """Time slots free for every listed student, between 08:00 and 20:00.

    students: TrombintIDs or full names. date: YYYY-MM-DD, default today.
    """
    if not students:
        return "Give at least one student."

    day = date or _today()
    resolved = await asyncio.gather(*(client.resolve_student(s) for s in students))

    student_ids = [sid for sid, _ in resolved if sid]
    names = [name for sid, name in resolved if sid]
    missing = [ref for ref, (sid, _) in zip(students, resolved) if not sid]
    if not student_ids:
        return "None of those students were found."

    agenda = await _agenda(student_ids, day)

    day_start, day_end = 8 * 60, 20 * 60
    busy = []
    for sid in student_ids:
        for e in agenda.get(str(sid), []):
            st = datetime.fromisoformat(e["start_time"])
            et = datetime.fromisoformat(e["end_time"])
            s_min = max(day_start, st.hour * 60 + st.minute)
            e_min = min(day_end, et.hour * 60 + et.minute)
            if s_min < e_min:
                busy.append((s_min, e_min))

    busy.sort()
    merged: List[Tuple[int, int]] = []
    for interval in busy:
        if not merged or merged[-1][1] < interval[0]:
            merged.append(interval)
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], interval[1]))

    free: List[Tuple[int, int]] = []
    curr = day_start
    for b_start, b_end in merged:
        if b_start > curr:
            free.append((curr, b_start))
        curr = max(curr, b_end)
    if curr < day_end:
        free.append((curr, day_end))

    out = [f"free {day} for {', '.join(names)}:"]
    if missing:
        out.append(f"not found: {', '.join(missing)}")
    if not free:
        out.append("no common slot between 08:00 and 20:00")
    else:
        out += [
            f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d} ({e - s}m)" for s, e in free
        ]
    return "\n".join(out)


@mcp.tool()
async def where_is_student(student: str, at: str = ""):
    """Infer where a student is: their classroom if they have a class, else their apartment.

    at: ISO datetime YYYY-MM-DDTHH:MM:SS, default now.
    """
    student_id, who = await client.resolve_student(student)
    if not student_id:
        return who

    if at:
        try:
            target = datetime.fromisoformat(at)
        except ValueError:
            return f"Invalid ISO datetime '{at}'."
    else:
        target = datetime.now()

    data, agenda = await asyncio.gather(
        client.get(f"/students/{student_id}"),
        _agenda([student_id], target.strftime("%Y-%m-%d")),
    )

    stamp = target.strftime("%Y-%m-%d %H:%M")
    for e in agenda.get(str(student_id), []):
        if datetime.fromisoformat(e["start_time"]) <= target <= datetime.fromisoformat(e["end_time"]):
            room = e.get("room") or "unspecified room"
            return f"{who} at {stamp}: in class {e['name']} ({e.get('type', 'course')}) in {room}"

    apt = data.get("apartment")
    place = f"apartment {apt} (Maisel)" if apt else "unknown (no apartment on file)"
    return f"{who} at {stamp}: no class, likely at {place}"


@mcp.tool()
async def find_available_rooms(start_time: str, end_time: str):
    """Rooms with no class scheduled in a window. ISO datetimes, e.g. '2026-07-28T14:00:00'."""
    rooms = await client.get(
        "/agenda/rooms/available", params={"start_time": start_time, "end_time": end_time}
    )
    if not rooms:
        return f"No room free between {start_time} and {end_time}."
    return _block(f"free {start_time} to {end_time}", sorted(rooms), 60)


@mcp.tool()
async def get_room_schedule(room_query: str, start_date: str, end_date: str):
    """Classes booked in rooms matching room_query (substring), between two YYYY-MM-DD dates."""
    events = await client.get(
        "/agenda/rooms/occupancy",
        params={"room_query": room_query, "start_date": start_date, "end_date": end_date},
    )
    if not events:
        return f"Nothing scheduled in rooms matching '{room_query}' from {start_date} to {end_date}."

    return _block(
        f"{room_query} {start_date} to {end_date}",
        [
            f"{datetime.fromisoformat(e['start_time']):%Y-%m-%d %H:%M}-{_hhmm(e['end_time'])} "
            f"{e['room']} {e['name']} ({e['type']})"
            for e in events
        ],
    )
