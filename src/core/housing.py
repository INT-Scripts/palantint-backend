"""Canonical legacy-plan assignment shared by ingestion and API projection."""

import re

from core.map_reference import reference_data
from core.places import normalized_name


def housing_plan(code, building=None, floor=None):
    reference = reference_data()
    definitions = reference["legacy_buildings"]
    code = str(code)
    if building not in definitions and re.fullmatch(r"[1-7]\d{3}", code):
        building = "U" + code[0]
    if building not in definitions or definitions[building]["category"] != "housing":
        return None
    valid = {row["value"] for row in definitions[building]["floors"]}
    value = normalized_name(str(floor or ""))
    if "rdc" in value or "rez" in value:
        value = "0"
    else:
        number = re.search(r"-?\d+(?:[.,]\d+)?", value)
        value = f"{float(number[0].replace(',', '.')):g}" if number else ""
    if value not in valid and re.fullmatch(r"\d{4}", code):
        value = code[1]
    for rule in reference.get("housing_floor_rules", []):
        if building == rule["building"] and value == rule["source_floor"]:
            if re.fullmatch(r"\d{4}", code):
                value = (
                    rule["upper"]
                    if int(code[2:]) >= rule["room_suffix_threshold"]
                    else rule["lower"]
                )
            else:
                return None
    return {"building": building, "floor": value} if value in valid else None
