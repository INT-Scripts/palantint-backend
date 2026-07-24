import logging
from collections import deque
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastmcp import FastMCP

from core.config import settings

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp")
logger.setLevel(logging.DEBUG)

# Configuration from settings
PALANTINT_URL = settings.MCP_PALANTINT_URL
MCP_SERVICE_TOKEN = settings.MCP_SERVICE_TOKEN

mcp = FastMCP("PalantINT")
mcp._mcp_server.name = "PalantINT"


class PalantINTClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def get_headers(self):
        if not MCP_SERVICE_TOKEN:
            return {}
        return {"Authorization": f"Bearer {MCP_SERVICE_TOKEN}"}

    async def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None):
        headers = await self.get_headers()
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def post(self, endpoint: str, json: Optional[Dict[str, Any]] = None):
        headers = await self.get_headers()
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            response = await client.post(url, json=json)
            response.raise_for_status()
            return response.json()

    async def find_student_id(self, trombint_id: str) -> Optional[str]:
        """Convert a TrombintID (string) to a UUID (string)."""
        search_data = await self.get("/search", params={"q": trombint_id})
        target = trombint_id.lower()
        for s in search_data.get("students", []):
            if s["trombint_id"].lower() == target:
                return s["id"]
        return None


client = PalantINTClient(PALANTINT_URL)


@mcp.prompt()
def tool_guide():
    """
    A comprehensive guide on how to use the PalantINT tools effectively.
    """
    return """
# PalantINT Tool Usage Guide

Use these tools to answer questions about campus life, people, and buildings.

## People & Social
1. **Identify**: Use `search_directory(query)` to find a person's `trombint_id`.
2. **Profile**: Use `get_student_profile(trombint_id)` for basic identity.
3. **Relationships**: Use `get_student_relationships(trombint_id)` to find friends and roommates.
4. **Socials**: Use `get_student_socials(trombint_id)` for social media handles (LinkedIn, GitHub, etc.).
5. **Notes**: Use `get_student_notes(trombint_id)` to see what people say about them.
6. **Pathfinding**: Use `find_shortest_path(trombint_id_1, trombint_id_2)` to discover how 2 students are connected.

## Clubs & Academics
- **Club Info**: Use `get_club_info(query)` for club description, office, and links.
- **Club Members**: Use `list_club_members(query)` to view club officers and members.
- **Class Roster**: Use `get_class_roster(query)` to view students in a class or promo group.

## Housing & Location
- **Specs**: Use `get_apartment_info(apartment_id)` (e.g., 'U3-101') to see room size and price.
- **Occupants**: Use `list_roommates(trombint_id)` to find who lives with someone.
- **Where Is Student**: Use `where_is_student(trombint_id, datetime_str)` to infer current location (classroom or apartment).

## Schedule & Availability
- **Today**: Use `get_student_schedule(trombint_id)` to see their classes today.
- **Find Time**: Use `find_common_free_slots(trombint_ids, date)` to coordinate meetings across students.

## Campus Status
- **Laundry**: Use `get_laundry_status()` for machine availability.
- **Map**: Use `get_map_location(query)` to find any room or student's home.
"""


@mcp.tool()
async def search_directory(query: str):
    """
    Search the directory for students or clubs. Returns IDs/Slugs for further lookup.
    """
    data = await client.get("/search", params={"q": query})

    output = []
    if data.get("students"):
        output.append("### Students Found")
        for s in data["students"]:
            output.append(
                f"- {s['first_name']} {s['last_name']} (TrombintID: {s['trombint_id']}, Apt: {s['apartment'] or 'N/A'})"
            )

    if data.get("clubs"):
        output.append("\n### Clubs Found")
        for c in data["clubs"]:
            output.append(f"- {c['name']} (Slug: {c['slug']})")

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
    student_ids = []
    found_map = {}

    for tid in trombint_ids:
        sid = await client.find_student_id(tid)
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

    graph = await client.get("/private/graph") if client.base_url.endswith("/api") else await client.get("/graph")
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
async def get_map_location(query: str):
    """
    Find coordinates or building/floor for a room or student.
    """
    data = await client.get("/maps/search", params={"q": query})
    if not data:
        return f"No location found for '{query}'."

    res = []
    for item in data:
        res.append(
            f"- {item['name']} ({item['type']}) - Building {item.get('building', 'N/A')}, Floor {item['floor']}"
        )
    return "\n".join(res)


if __name__ == "__main__":
    # Start the MCP server using SSE transport for remote access
    # This turns it into a web server listening on port 8001
    mcp.run(transport="sse")
