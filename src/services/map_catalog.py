"""Public-safe plan catalog, projected from seeded Location metadata."""

from sqlalchemy import select

from db.models import Location


async def plan_catalog(db):
    rows = (
        (
            await db.execute(
                select(Location).where(
                    Location.is_active.is_(True),
                    Location.kind.in_(("BUILDING", "FLOOR")),
                )
            )
        )
        .scalars()
        .all()
    )
    buildings = {
        row.id: row
        for row in rows
        if row.kind == "BUILDING" and (row.attributes or {}).get("legacy_plan")
    }
    from core.laundry import LAUNDRY_URLS

    catalog = {
        "housing": {},
        "foyer": {},
        "all": {},
        "laundry": [code.upper() for code in LAUNDRY_URLS],
    }
    for building in sorted(buildings.values(), key=lambda row: row.code):
        floors = []
        for row in rows:
            metadata = (row.attributes or {}).get("legacy_plan")
            if row.kind == "FLOOR" and row.parent_id == building.id and metadata:
                assets = metadata.get("assets", {})
                # Preserve authoritative custom paths when projecting archival
                # SVG metadata to the generated GeoJSON transport.
                geojson = assets.get("geojson")
                if not geojson:
                    source = assets.get(
                        "svg", f"/api/assets/plans/{building.code}_{row.code}.svg"
                    )
                    geojson = source.removesuffix(".svg") + ".geojson"
                floors.append(
                    {
                        "value": row.code,
                        "label": metadata["label"],
                        "assets": {"geojson": geojson},
                    }
                )
        floors.sort(key=lambda row: float(row["value"]))
        if not floors:
            continue
        category = building.attributes["legacy_plan"]["category"]
        catalog["all"][building.code] = floors
        if category in ("housing", "foyer"):
            catalog[category][building.code] = floors
    return catalog
