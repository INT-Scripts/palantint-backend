import logging
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
4. **Notes**: Use `get_student_notes(trombint_id)` to see what people say about them.

## Housing
- **Specs**: Use `get_apartment_info(apartment_id)` (e.g., 'U3-101') to see room size and price.
- **Occupants**: Use `list_roommates(trombint_id)` to find who lives with someone.

## Schedule & Availability
- **Today**: Use `get_student_schedule(trombint_id)` to see their classes today.
- **Find Time**: Use `find_common_free_slots(trombint_ids, date)` to coordinate meetings.

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
