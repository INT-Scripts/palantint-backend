"""Housing and campus facilities, plus the reference lists other tools accept."""

from typing import Annotated, Literal

from pydantic import Field

from ..client import APARTMENTS_TTL, client
from ..format import call, fields, joined, page
from ..inputs import Offset, clamp


async def get_apartment_info(
    apartment: Annotated[str, Field(description="Maisel apartment code, e.g. '7413'.")],
) -> str:
    """Specs of a Maisel residence apartment: building, floor, type, size, rent and housing aid."""
    details = await client.cached_get("/students/apartments/details", APARTMENTS_TTL)
    code = apartment.strip().upper()
    apt = details.get(code)
    if not apt:
        return f"No apartment {apartment}. Codes look like '7413' (building digit first)."

    return f"apt {code}: " + fields(
        ("building", apt.get("Bâtiment")),
        ("floor", apt.get("Etage")),
        ("type", apt.get("Type")),
        ("size", apt.get("Superficie")),
        ("price", apt.get("Tarif")),
        ("aid scholarship", apt.get("Allocation boursier")),
        ("aid standard", apt.get("Allocation non boursier")),
    )


async def get_laundry_status(
    building: Annotated[str, Field(description="Residence building, e.g. 'U3'. Empty for every building.")] = "",
) -> str:
    """Live washer and dryer availability in the Maisel residence laundries."""
    data = await client.get("/laundry/status", params={"building": building} if building else None)

    out = []
    for bldg, machines in (data or {}).items():
        # Two lines per building listing machine numbers, rather than one line per machine.
        dryers = [m for m in machines or [] if m.get("machine_type") == "sl"]
        washers = [m for m in machines or [] if m.get("machine_type") != "sl"]
        for kind, group in (("washers", washers), ("dryers", dryers)):
            if not group:
                continue
            free = [str(m.get("machine_nbr")) for m in group if not m.get("started_at")]
            busy = [str(m.get("machine_nbr")) for m in group if m.get("started_at")]
            out.append(f"{bldg.upper()} {kind}: " + fields(("free", ",".join(free)), ("busy", ",".join(busy))))

    if out:
        return "\n".join(out)
    return f"No laundry data for {building}. Buildings are U3 to U7." if building else "Laundry status is unavailable right now."


async def list_reference(
    kind: Annotated[
        Literal["rooms", "buildings", "promos", "schools", "relationship_types"],
        Field(description="rooms (names for the room tools), buildings (residences and floors), promos and schools (values for search_directory filters), relationship_types."),
    ],
    offset: Offset = 0,
) -> str:
    """Valid values for other tools' arguments: room names, buildings, promos, schools, relationship types."""
    if kind == "rooms":
        rooms = await client.get("/agenda/rooms/list")
        return page("rooms", sorted(rooms or []), clamp(offset, 0, 100_000), 80, call("list_reference", kind="rooms"))

    if kind == "buildings":
        data = await client.get("/maps/buildings") or {}
        return page("buildings", [f"{b}: floors {', '.join(map(str, floors))}" for b, floors in data.items()])

    if kind in ("promos", "schools"):
        data = await client.get("/students/filters") or {}
        values = [str(v) for v in data.get("promos" if kind == "promos" else "ecoles") or []]
        return joined(kind, values, 60) or f"{kind}: none"

    types = await client.get("/relationship-types")
    return joined("relationship types", [t["name"] for t in types or []], 40) or "relationship types: none"
