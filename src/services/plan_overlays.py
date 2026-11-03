"""Server-owned associations between calibrated drawings and geographic buildings."""


def plan_overlays(catalog, buildings, markers):
    overlays, warnings = [], []
    for code, floors in catalog["all"].items():
        candidates = [
            m
            for m in markers
            if m["bldg_id"] == code and len(m.get("footprint", [])) >= 3
        ]
        if len(candidates) > 1:
            canonical = [
                m
                for m in candidates
                if m.get("label", "").casefold() == code.casefold()
            ]
            candidates = canonical if len(canonical) == 1 else candidates
        if len(candidates) != 1:
            if candidates:
                warnings.append({"building": code, "reason": "ambiguous_alignment"})
            continue
        marker = candidates[0]
        ids = [
            b["id"]
            for b in buildings
            if b["name"].casefold() == code.casefold()
            or any(r["code"] == code for r in b.get("residences", []))
        ]
        overlays.append(
            {
                "id": marker["id"],
                "building": code,
                "campus_building_ids": ids,
                "campus_requirements": {b["id"]: [r["code"] for r in b.get("residences", [])] or [code] for b in buildings if b["id"] in ids},
                "floors": floors,
                "wireframe": marker["wireframe"],
            }
        )
    return {"overlays": overlays, "warnings": warnings}
