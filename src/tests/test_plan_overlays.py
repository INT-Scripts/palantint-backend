from services.plan_overlays import plan_overlays


def test_only_calibrated_catalog_buildings_replace_geographic_plans():
    catalog = {
        "all": {"U1": [{"value": "0"}], "U7": [{"value": "1"}], "U6": [{"value": "1"}]}
    }
    buildings = [
        {"id": "combined", "name": "Résidences", "residences": [{"code": "U1"}]},
        {"id": "u7", "name": "U7"},
    ]

    def marker(id, code, label):
        return {
            "id": id,
            "bldg_id": code,
            "label": label,
            "footprint": [[0, 0, 0], [1, 0, 0], [1, 0, 1]],
            "wireframe": {"position": [10, 2, 20], "rotation": [0, 1, 0], "scale": 7},
        }

    result = plan_overlays(
        catalog,
        buildings,
        [
            marker("u1", "U1", "U1"),
            marker("default", "U1", "Autre"),
            marker("u7", "U7", "U7"),
        ],
    )
    assert [o["id"] for o in result["overlays"]] == ["u1", "u7"]
    assert result["overlays"][0]["campus_building_ids"] == ["combined"]
    assert result["overlays"][0]["wireframe"]["rotation"] == [0, 1, 0]
    ambiguous = plan_overlays(
        catalog, buildings, [marker("a", "U1", "A"), marker("b", "U1", "B")]
    )
    assert ambiguous["overlays"] == []
    assert ambiguous["warnings"] == [
        {"building": "U1", "reason": "ambiguous_alignment"}
    ]
