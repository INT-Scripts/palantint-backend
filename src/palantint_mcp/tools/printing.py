"""Print quota sharing (followme): aggregate figures only, never documents or names."""

from ..client import client
from ..format import fields


async def get_print_sharing_stats() -> str:
    """Totals of PalantINT print quota sharing: donors, active grants, pages printed and reserved, requests by status."""
    data = await client.get("/print/admin/stats") or {}
    statuses = data.get("requests_by_status") or {}
    by_status = ", ".join(f"{k.lower()} {v}" for k, v in sorted(statuses.items())) or "none"
    return "print sharing: " + fields(
        ("donors sharing", data.get("sharing_donors")),
        ("active grants", data.get("active_grants")),
        ("pages printed", data.get("pages_printed")),
        ("pages reserved", data.get("pages_reserved")),
        ("requests", by_status),
    )
