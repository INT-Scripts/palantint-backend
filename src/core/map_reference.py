"""Versioned reference inputs; consumed by seeds and ingestion, never the UI."""

import copy
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def reference_data():
    return json.loads(
        Path(__file__).with_name("map_reference.json").read_text(encoding="utf-8")
    )


def curate_collection(collection, extra_rules=()):
    result = copy.deepcopy(collection)
    rules = [*reference_data()["curations"], *extra_rules]
    for feature in result["features"]:
        for rule in rules:
            if feature["id"] not in rule["feature_ids"]:
                continue
            properties = feature["properties"]
            properties.update(rule["properties"])
            properties["kind"] = (
                "room" if feature["id"] == rule["primary_feature_id"] else "poi"
            )
            properties["curation"] = {
                key: rule[key]
                for key in (
                    "id",
                    "source_code",
                    "reason",
                    "location",
                    "aliases",
                    "primary_feature_id",
                )
            }
    return result
