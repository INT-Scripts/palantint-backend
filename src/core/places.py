"""Shared, server-side place normalization and presentation metadata."""

import re
import unicodedata


def normalized_name(value):
    return (
        "".join(
            c
            for c in unicodedata.normalize("NFD", value or "")
            if not unicodedata.combining(c)
        )
        .casefold()
        .strip()
    )


def room_alias(value):
    return re.sub(
        r"\s+(?:(?:sc|si|sec)\s*)?\d+\s*pl(?:aces)?\b.*$", "", normalized_name(value)
    ).strip()


# A room code as the timetable writes it: an optional wing digit, a building
# letter and three digits (B313, 3A401), or a range of joined rooms ("A001-002").
ROOM_CODE = re.compile(r"(?<![a-z0-9])(\d?[a-z])(\d{3})(?:-(\d{3}))?(?![a-z0-9])")


def room_codes(value):
    """Room codes named by a label, one tuple per code or range, in order."""
    return [
        (prefix + first, *([prefix + last] if last else []))
        for prefix, first, last in ROOM_CODE.findall(normalized_name(value))
    ]


def unpadded(value):
    """Room alias without leading zeros in standalone numbers ("amphi 010" → "amphi 10")."""
    return re.sub(r"\b0+(\d)", r"\1", room_alias(value))


def place_category(properties):
    if properties.get("category") in {"housing", "teaching", "services"}:
        return properties["category"]
    name = normalized_name(
        " ".join(
            str(properties.get(key) or "")
            for key in ("name", "category_name", "tag_names")
        )
    )
    if re.search(r"\bu[1-7]\b|logement|residence|apartment", name):
        return "housing"
    if re.search(r"classroom|amphi|enseignement|salle de cours|lecture", name):
        return "teaching"
    return "services"


def mark_destinations(features):
    """Only collapse a label point when its corresponding room is unambiguous."""

    def key(feature):
        props = feature["properties"]
        return (
            props.get("name"),
            props.get("building_id"),
            tuple(props.get("levels", [])),
        )

    room_keys = {}
    room_ids = set()
    for feature in features:
        props = feature["properties"]
        if props.get("kind") == "room" and (props.get("name") or "").strip():
            room_keys[key(feature)] = room_keys.get(key(feature), 0) + 1
            if props.get("location_id"):
                room_ids.add(props["location_id"])
    for feature in features:
        props = feature["properties"]
        selectable = bool((props.get("name") or "").strip()) and props.get("kind") in {
            "building",
            "room",
            "poi",
        }
        if props.get("kind") == "poi":
            if props.get("location_id"):
                selectable = selectable and props["location_id"] not in room_ids
            elif room_keys.get(key(feature), 0) == 1:
                selectable = False
        props["selectable"] = selectable
    return features
