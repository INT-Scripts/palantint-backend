"""Derive the geographic-to-scan transform from authoritative calibration markers."""

import math

RADIUS = 6378137


def project(point, origin):
    return (
        (point[0] - origin[0])
        * math.pi
        / 180
        * RADIUS
        * math.cos(origin[1] * math.pi / 180),
        -(point[1] - origin[1]) * math.pi / 180 * RADIUS,
    )


def polygon_center(ring):
    area = x = y = 0
    for p, q in zip(ring, ring[1:] + ring[:1]):
        cross = p[0] * q[1] - q[0] * p[1]
        area += cross
        x += (p[0] + q[0]) * cross
        y += (p[1] + q[1]) * cross
    if abs(area) < 1e-12:
        return [
            sum(p[0] for p in ring) / len(ring),
            sum(p[1] for p in ring) / len(ring),
        ]
    return [x / (3 * area), y / (3 * area)]


def fit_registration(anchors):
    if len(anchors) < 3:
        return None
    origin = anchors[0]["geographic"]
    source = [project(a["geographic"], origin) for a in anchors]

    def mean(values):
        return sum(values) / len(values)

    sx, sz = (mean([p[i] for p in source]) for i in (0, 1))
    tx, tz = (mean([a["scene"][i] for a in anchors]) for i in (0, 1))
    dot = cross = spread = 0
    for (x, z), anchor in zip(source, anchors):
        u, v = anchor["scene"][0] - tx, anchor["scene"][1] - tz
        dot += (x - sx) * u + (z - sz) * v
        cross += (x - sx) * v - (z - sz) * u
        spread += (x - sx) ** 2 + (z - sz) ** 2
    if spread < 100:
        return None
    a, b = dot / spread, cross / spread
    scale = math.hypot(a, b)
    if not math.isfinite(scale) or not 0.1 <= scale <= 10:
        return None
    offset_x, offset_z = tx - a * sx + b * sz, tz - b * sx - a * sz
    error = (
        math.sqrt(
            mean(
                [
                    (a * x - b * z + offset_x - anchor["scene"][0]) ** 2
                    + (b * x + a * z + offset_z - anchor["scene"][1]) ** 2
                    for (x, z), anchor in zip(source, anchors)
                ]
            )
        )
        / scale
    )
    if error > 15:
        return None
    return {
        "origin": origin,
        "a": a,
        "b": b,
        "tx": offset_x,
        "tz": offset_z,
        "scale": scale,
        "altitude": mean([a["altitude"] for a in anchors]),
        "anchors": len(anchors),
        "error": error,
    }


def registration_from_markers(features, markers):
    anchors = []
    for feature in features:
        properties = feature["properties"]
        if (
            properties.get("kind") != "building"
            or not properties.get("name")
            or not properties.get("label_coordinates")
        ):
            continue
        codes = properties["name"].lower().split()
        matches = [
            [
                marker
                for marker in markers
                if marker["bldg_id"].lower() == code
                and (not marker.get("label") or marker["label"].lower() == code)
            ]
            for code in codes
        ]
        if not matches or any(
            len(group) != 1 or len(group[0].get("footprint", [])) < 3
            for group in matches
        ):
            continue
        total = x = z = 0
        footprints = [group[0]["footprint"] for group in matches]
        for footprint in footprints:
            ring = [[p[0], p[2]] for p in footprint]
            center = polygon_center(ring)
            area = (
                abs(
                    sum(
                        p[0] * q[1] - q[0] * p[1]
                        for p, q in zip(ring, ring[1:] + ring[:1])
                    )
                )
                / 2
            )
            total += area
            x += center[0] * area
            z += center[1] * area
        if not total:
            continue
        origin = properties["label_coordinates"]
        if feature["geometry"]["type"] == "Polygon":
            center = polygon_center(
                [project(p, origin) for p in feature["geometry"]["coordinates"][0]]
            )
            geographic = [
                origin[0]
                + center[0]
                / (RADIUS * math.cos(origin[1] * math.pi / 180))
                * 180
                / math.pi,
                origin[1] - center[1] / RADIUS * 180 / math.pi,
            ]
        else:
            geographic = origin
        points = [p for ring in footprints for p in ring]
        anchors.append(
            {
                "geographic": geographic,
                "scene": [x / total, z / total],
                "altitude": sum(p[1] for p in points) / len(points),
            }
        )
    return fit_registration(anchors)
