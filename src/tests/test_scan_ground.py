import json
import struct

from services.scan_ground import ground_height, scan_ground


def write_tile(directory, name, points, translation=(0, 0, 0), draco=False):
    (directory / f"{name}.bin").write_bytes(b"".join(struct.pack("<3f", *point) for point in points))
    document = {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["KHR_draco_mesh_compression"] if draco else [],
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"children": [1], "translation": list(translation)}, {"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(points), "type": "VEC3"}],
        "bufferViews": [{"buffer": 0, "byteLength": len(points) * 12}],
        "buffers": [{"uri": f"{name}.bin", "byteLength": len(points) * 12}],
    }
    (directory / f"{name}.gltf").write_text(json.dumps(document))


def campus(ground, roof_share=0.4):
    """Ground grid over 4 cells of 60 m, with a share of roof points 9 m higher."""
    points = []
    for i in range(120):
        for j in range(30):
            x, z = i * 2.0, j * 4.0
            points.append((x, ground + (9.0 if (i + j) % 10 < roof_share * 10 else 0.0), z))
    return points


def test_ground_is_the_low_surface_under_roofs(tmp_path):
    # Node translation: the tile's own ground is at -2 + -4 = -6 in the scene.
    write_tile(tmp_path, "tile_1", campus(-2.0), translation=(0, -4.0, 0))
    write_tile(tmp_path, "tile_2", [(0, 50.0, 0)] * 50, draco=True)
    assert abs(scan_ground(tmp_path) - -6.0) < 1e-3


def test_ground_ignores_sparse_cells_and_needs_several_cells():
    sparse = [(1000.0, -40.0, 1000.0)] * 10
    assert abs(ground_height(campus(1.5) + sparse) - 1.5) < 1e-3
    assert ground_height([(0.0, 0.0, 0.0)] * 500) is None


def test_no_scan_no_ground(tmp_path):
    assert scan_ground(tmp_path / "missing") is None
    assert scan_ground(tmp_path) is None
