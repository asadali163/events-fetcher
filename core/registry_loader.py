from pathlib import Path

import yaml

REGISTRY_PATH = Path(__file__).parent / "registry.yaml"


def load_registry():
    with open(REGISTRY_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def get_sources_for_country(country_code):
    country_code = country_code.upper()
    matches = []
    for source in load_registry():
        coverage = source.get("coverage", [])
        if coverage == "global" or country_code in coverage:
            matches.append(source)
    return matches


if __name__ == "__main__":
    import sys

    country = sys.argv[1] if len(sys.argv) > 1 else "FR"
    matches = get_sources_for_country(country)
    # print(matches)
    print(f"Sources covering {country}:")
    for m in matches:
        print(f" - {m['id']} ({m['type']})")
