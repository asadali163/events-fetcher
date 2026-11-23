"""Loads core/pipeline.yaml: per-city schedule + enrichment settings, each
city merged over `defaults` (nested dicts merged key by key)."""

import copy
from pathlib import Path

import yaml

PIPELINE_PATH = Path(__file__).parent / "pipeline.yaml"


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_pipeline():
    with open(PIPELINE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_city_config(city):
    data = load_pipeline()
    cities = data.get("cities") or {}
    if city not in cities:
        raise ValueError(
            f"'{city}' isn't in core/pipeline.yaml. Add it under `cities:` "
            f"(and to core/cities.yaml with its country code)."
        )
    config = _merge(data.get("defaults") or {}, cities[city] or {})
    config["city"] = city
    return config


def list_cities(enabled_only=True):
    cities = (load_pipeline().get("cities") or {})
    return [
        name for name, cfg in cities.items()
        if not enabled_only or (cfg or {}).get("enabled", True)
    ]


if __name__ == "__main__":
    import json

    for name in list_cities(enabled_only=False):
        print(json.dumps(get_city_config(name), indent=2, ensure_ascii=False))
