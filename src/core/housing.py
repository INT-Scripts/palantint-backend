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


def amount(value):
    """Maisel publishes display strings such as "550€" or "22 m²"."""
    if isinstance(value, (int, float)):
        return value
    match = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    if not match:
        return None
    number = float(match[0].replace(",", "."))
    return int(number) if number.is_integer() else number


def apartment_details(attributes):
    """Typed view of an APARTMENT ``Location.attributes`` for the private map."""
    attributes = attributes or {}
    rent = amount(attributes.get("price"))
    allowances = {
        "scholarship": amount(attributes.get("alloc_boursier")),
        "non_scholarship": amount(attributes.get("alloc_non_boursier")),
    }
    return {
        "type": attributes.get("type"),
        "floor": attributes.get("floor"),
        "surface_m2": amount(attributes.get("surface")),
        "rent": rent,
        "allowances": allowances,
        "net_rent": {
            key: rent - value if rent is not None and value is not None else rent
            for key, value in allowances.items()
        },
    }
