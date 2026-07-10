from pathlib import Path

import yaml

CITIES_PATH = Path(__file__).parent / "cities.yaml"


def load_cities():
    with open(CITIES_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("cities", {})


def get_country_for_city(city_name):
    cities = load_cities()
    if city_name not in cities:
        raise ValueError(
            f"'{city_name}' isn't in cities.yaml yet. "
            f"Add a line like '{city_name}: XX' (its ISO country code) and try again."
        )
    return cities[city_name]


if __name__ == "__main__":
    import sys

    city = sys.argv[1] if len(sys.argv) > 1 else "Chicago"
    print(f"{city} -> {get_country_for_city(city)}")
