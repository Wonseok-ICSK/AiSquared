"""Build backend/app/core/data/airports.json from OurAirports (public domain).

Keeps large/medium airports that have an IATA code and scheduled service and writes a
compact list of ``{code, name, city, country, lat, lon}`` sorted by code.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\build_airports.py [--source airports.csv]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

import httpx

SOURCE_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OUTPUT = Path(__file__).resolve().parent.parent / "backend" / "app" / "core" / "data" / "airports.json"
KEEP_TYPES = {"large_airport", "medium_airport"}


def fetch_csv(url: str = SOURCE_URL) -> str:
    """Download the OurAirports CSV as text."""
    response = httpx.get(url, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    return response.content.decode("utf-8")


def parse_airports(csv_text: str) -> list[dict[str, object]]:
    """Filter and compact OurAirports rows."""
    by_code: dict[str, dict[str, object]] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        code = (row.get("iata_code") or "").strip().upper()
        if (
            row.get("type") not in KEEP_TYPES
            or row.get("scheduled_service") != "yes"
            or len(code) != 3
            or not code.isalpha()
        ):
            continue
        entry = {
            "code": code,
            "name": row["name"].strip(),
            "city": (row.get("municipality") or "").strip(),
            "country": row["iso_country"].strip().upper(),
            "lat": round(float(row["latitude_deg"]), 4),
            "lon": round(float(row["longitude_deg"]), 4),
        }
        # Duplicate IATA codes are rare; prefer a large airport over a medium one.
        if code not in by_code or row["type"] == "large_airport":
            by_code[code] = entry
    return [by_code[code] for code in sorted(by_code)]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="local airports.csv instead of downloading")
    args = parser.parse_args(argv)
    csv_text = args.source.read_text(encoding="utf-8") if args.source else fetch_csv()
    airports = parse_airports(csv_text)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    lines = (json.dumps(a, ensure_ascii=False, separators=(",", ":")) for a in airports)
    OUTPUT.write_text("[\n" + ",\n".join(lines) + "\n]\n", encoding="utf-8")  # one per line
    print(f"wrote {len(airports)} airports to {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
