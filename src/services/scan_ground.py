"""Ground level of the campus photogrammetry, measured on the scan itself.

The calibration markers place the scan horizontally; their clicked heights mix
ground and roofs, so they are a poor vertical reference. The ground is instead
the low surface of the scan: in each 60 m cell, the 3rd percentile of vertex
heights (the ground, under roofs and trees), then the median over cells, which
discards edges, pits and noise. Draco-compressed tiles are skipped: the plain
ones cover the campus widely enough for one level.
"""

import json
import math
import statistics
import struct
from functools import lru_cache
from pathlib import Path

CELL = 60.0
# Vertices sampled per mesh, at most: enough for a percentile, cheap to read.
SAMPLES = 4000
MIN_CELL_SAMPLES = 200
MIN_CELLS = 3
LOW_PERCENTILE = 0.03


def multiply(m, n):
    """4×4 matrices as column-major lists, as glTF stores them."""
    return [sum(m[k * 4 + r] * n[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]


def node_matrix(node):
    if "matrix" in node:
        return list(node["matrix"])
    tx, ty, tz = node.get("translation", (0, 0, 0))
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    rotation = [
        1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0,
        2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0,
        2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0,
        0, 0, 0, 1,
    ]
    scale = [sx, 0, 0, 0, 0, sy, 0, 0, 0, 0, sz, 0, 0, 0, 0, 1]
    translation = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, tx, ty, tz, 1]
    return multiply(translation, multiply(rotation, scale))


def tile_points(path):
    """Sampled vertices of one plain glTF tile, in scan scene coordinates (x, y, z)."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if "KHR_draco_mesh_compression" in document.get("extensionsUsed", []):
        return []
    buffers = [(path.parent / buffer["uri"]).read_bytes() if buffer.get("uri") else b"" for buffer in document.get("buffers", [])]
    points = []

    def walk(index, parent):
        node = document["nodes"][index]
        matrix = multiply(parent, node_matrix(node))
        if "mesh" in node:
            for primitive in document["meshes"][node["mesh"]]["primitives"]:
                accessor = document["accessors"][primitive["attributes"]["POSITION"]]
                if accessor.get("componentType") != 5126 or "bufferView" not in accessor:
                    continue
                view = document["bufferViews"][accessor["bufferView"]]
                data = buffers[view["buffer"]]
                start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
                stride = view.get("byteStride", 12)
                step = max(1, accessor["count"] // SAMPLES)
                for i in range(0, accessor["count"], step):
                    x, y, z = struct.unpack_from("<3f", data, start + i * stride)
                    points.append((
                        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
                        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
                        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
                    ))
        for child in node.get("children", []):
            walk(child, matrix)

    identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    for root in document["scenes"][document.get("scene", 0)]["nodes"]:
        walk(root, identity)
    return points


def ground_height(points):
    """Scene height of the ground, or None without enough well-covered cells."""
    cells = {}
    for x, y, z in points:
        cells.setdefault((math.floor(x / CELL), math.floor(z / CELL)), []).append(y)
    lows = []
    for heights in cells.values():
        if len(heights) >= MIN_CELL_SAMPLES:
            heights.sort()
            lows.append(heights[int(len(heights) * LOW_PERCENTILE)])
    return statistics.median(lows) if len(lows) >= MIN_CELLS else None


@lru_cache(maxsize=2)
def _scan_ground(directory, signature):
    points = []
    for path in sorted(Path(directory).glob("*.gltf")):
        try:
            points.extend(tile_points(path))
        except (OSError, ValueError, KeyError, IndexError, struct.error):
            continue
    return ground_height(points)


def scan_ground(directory):
    """Ground height of the scan in its scene units, cached until a tile changes."""
    directory = Path(directory)
    if not directory.is_dir():
        return None
    signature = tuple(sorted((path.name, path.stat().st_mtime_ns) for path in directory.glob("*.gltf")))
    return _scan_ground(str(directory), signature) if signature else None
