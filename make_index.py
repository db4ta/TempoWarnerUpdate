#!/usr/bin/env python3
"""Writes index.json for the TempoWarner app from the built region databases.

    python make_index.py <tag> regions/  > regions/index.json
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

NAMES = {
    "baden-wuerttemberg": "Baden-Württemberg", "bayern": "Bayern", "berlin": "Berlin",
    "brandenburg": "Brandenburg", "bremen": "Bremen", "hamburg": "Hamburg", "hessen": "Hessen",
    "mecklenburg-vorpommern": "Mecklenburg-Vorpommern", "niedersachsen": "Niedersachsen",
    "nordrhein-westfalen": "Nordrhein-Westfalen", "rheinland-pfalz": "Rheinland-Pfalz", "saarland": "Saarland",
    "sachsen": "Sachsen", "sachsen-anhalt": "Sachsen-Anhalt", "schleswig-holstein": "Schleswig-Holstein",
    "thueringen": "Thüringen", "austria": "Österreich", "switzerland": "Schweiz",
}


def main() -> None:
    tag, folder = sys.argv[1], Path(sys.argv[2])
    regions = []
    for f in sorted(folder.glob("*.sqlite")):
        db = sqlite3.connect(f)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        db.close()
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        regions.append({"id": f.stem, "name": NAMES.get(f.stem, f.stem), "country": meta.get("country", "DE"),
                        "file": f.name, "bytes": f.stat().st_size,
                        "sha256": sha, "osm_date": meta.get("osm_date"), "built": meta.get("built"),
                        "tiles": int(meta.get("tiles", 0)), "bbox": meta.get("bbox")})
    json.dump({"format": 1, "tag": tag, "regions": regions}, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
