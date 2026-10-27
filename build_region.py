#!/usr/bin/env python3
"""Builds an offline road database for TempoWarner from a Geofabrik OSM extract.

    python build_region.py                                        # Baden-Wuerttemberg
    python build_region.py --region europe/germany/bayern
    python build_region.py --check 48.7784,9.1800   # nearest roads at a point (Stuttgart, Schlossplatz)

Pipeline: download .osm.pbf -> pyosmium (drivable highways with node locations) ->
clip ways into the app's tiles (0.01 deg lat x 0.015 deg lon) -> SQLite `tiles(y, x, data)`,
data = raw-deflate JSON {"w": [{i,h,n,m,f,b,o,e,c}]} with c = [lat*1e6, lon*1e6, ...],
e = 1 when m was inferred (zone 30 from connected streets).
Setup: pip install -r requirements.txt
"""
import argparse
import json
import math
import re
import sqlite3
import sys
import time
import urllib.request
import zlib
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
OUT = HERE / "regions"
LAT_STEP, LON_STEP = 0.01, 0.015  # Must match TileKey in RoadStore.swift
PAD = 0.0003                      # Overlap so segments along a tile edge are found from both sides
HIGHWAYS = ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
            "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
            "road", "service")
SKIP_SERVICE = {"parking_aisle", "driveway", "drive-through"}
ZONE_ROUNDS = 3  # Zone-30 inference spreads at most this many streets away from a tagged one
MAX_AGE_DAYS = 7


def download(region: str) -> Path:
    CACHE.mkdir(exist_ok=True)
    name = region.split("/")[-1]
    pbf = CACHE / f"{name}-latest.osm.pbf"
    if pbf.exists() and time.time() - pbf.stat().st_mtime < MAX_AGE_DAYS * 86400:
        print(f"[1/5] {pbf.name} vorhanden ({pbf.stat().st_size // 2**20} MB)")
        return pbf
    url = f"https://download.geofabrik.de/{region}-latest.osm.pbf"
    print(f"[1/5] Lade {url}")
    tmp = pbf.with_suffix(".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TempoWarner-build/1.0"})) as r, \
            open(tmp, "wb") as f:
        total, done, last = int(r.headers.get("Content-Length", 0)), 0, 0.0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if time.time() - last > 5:
                last = time.time()
                print(f"      {done // 2**20} / {total // 2**20} MB", flush=True)
    tmp.rename(pbf)
    return pbf


def roads(pbf: Path) -> list:
    """[tags, [(lon, lat), ...], node refs (residential only)] for every drivable highway way."""
    import osmium
    print("[2/5] Straßen lesen (pyosmium, mit Knotenkoordinaten)")
    fp = (osmium.FileProcessor(str(pbf))
          .with_locations("flex_mem")
          .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
          .with_filter(osmium.filter.TagFilter(*[("highway", h) for h in HIGHWAYS])))
    result = []
    for w in fp:
        coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        tags = dict(w.tags)
        tags["@id"] = w.id
        refs = [n.ref for n in w.nodes] if tags.get("highway") == "residential" else None
        result.append([tags, coords, refs])
    return result


ZONE_RE = re.compile(r"zone:?(\d+)$|^[A-Z]{2}:(\d+)$")


def zone_speed(tags: dict) -> str | None:
    """Explicit zone tagging without maxspeed, e.g. zone:maxspeed=DE:30, source:maxspeed=DE:zone30."""
    for k in ("zone:maxspeed", "source:maxspeed", "maxspeed:type"):
        m = ZONE_RE.search(tags.get(k, ""))
        if m:
            return m.group(1) or m.group(2)
    return None


def infer_zones(ways: list) -> None:
    """German 30 zones are often only partly tagged in OSM. A residential street without
    maxspeed whose connected residential streets mostly have 30 gets "30, estimated"."""
    print("[3/5] Tempo-30-Zonen ableiten")
    explicit = 0
    for tags, _, _ in ways:
        if "maxspeed" not in tags and (z := zone_speed(tags)):
            tags["maxspeed"] = z
            explicit += 1
    res = [w for w in ways if w[2] is not None]
    by_node: dict[int, list[int]] = defaultdict(list)
    for i, (_, _, refs) in enumerate(res):
        for r in refs:
            by_node[r].append(i)
    speed = [t.get("maxspeed") for t, _, _ in res]  # Tagged or inferred
    inferred = 0
    for _ in range(ZONE_ROUNDS):
        new = {}
        for i, (tags, _, refs) in enumerate(res):
            if speed[i] is not None:
                continue
            votes = defaultdict(int)
            for n in {j for r in refs for j in by_node[r] if j != i}:
                if speed[n] is not None:
                    votes[speed[n]] += 1
            n30 = votes.pop("30", 0)
            if n30 and n30 > sum(votes.values()):
                new[i] = "30"
        if not new:
            break
        for i, v in new.items():
            speed[i] = v
            res[i][0]["@zone"] = v
        inferred += len(new)
    print(f"      {explicit} Straßen mit Zonen-Tag, {inferred} Wohnstraßen als 30 geschätzt")


def oneway(tags: dict) -> int:
    v = tags.get("oneway")
    if v in ("yes", "1", "true"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if v in ("no", "false", "0"):
        return 0
    implied = tags.get("highway") == "motorway" or tags.get("junction") in ("roundabout", "circular")
    return 1 if implied else 0


def tile_range(a: float, b: float, step: float) -> range:
    return range(math.floor((min(a, b) - PAD) / step), math.floor((max(a, b) + PAD) / step) + 1)


def build_tiles(ways: list) -> dict:
    tiles: dict[tuple[int, int], list] = defaultdict(list)
    n_ways = 0
    for p, coords, _ in ways:
        if p.get("highway") == "service" and p.get("service") in SKIP_SERVICE:
            continue
        if p.get("access") in ("no", "private") or p.get("area") == "yes":
            continue
        if len(coords) < 2:
            continue
        meta = {"i": int(p["@id"]), "h": p["highway"], "n": p.get("name") or p.get("ref"),
                "m": p.get("maxspeed"), "f": p.get("maxspeed:forward"), "b": p.get("maxspeed:backward"),
                "o": oneway(p)}
        if "@zone" in p:
            meta.update(m=p["@zone"], e=1)  # e = estimated (orange ring in the app)
        meta = {k: v for k, v in meta.items() if v not in (None, 0)}
        # Consecutive segments in the same tile form one polyline piece
        runs: dict[tuple[int, int], list[list[int]]] = {}
        last_seg: dict[tuple[int, int], int] = {}
        for s in range(len(coords) - 1):
            (lo1, la1), (lo2, la2) = coords[s], coords[s + 1]
            for y in tile_range(la1, la2, LAT_STEP):
                for x in tile_range(lo1, lo2, LON_STEP):
                    key = (y, x)
                    if last_seg.get(key) == s - 1:
                        runs[key][-1].append(s + 1)
                    else:
                        runs.setdefault(key, []).append([s, s + 1])
                    last_seg[key] = s
        for key, pieces in runs.items():
            for idx in pieces:
                c = []
                for k in idx:
                    c += [round(coords[k][1] * 1e6), round(coords[k][0] * 1e6)]
                tiles[key].append(dict(meta, c=c))
        n_ways += 1
        if n_ways % 100000 == 0:
            print(f"      {n_ways} Straßen ...", flush=True)
    print(f"[4/5] {n_ways} Straßen in {len(tiles)} Kacheln aufgeteilt")
    return tiles


def write_db(tiles: dict, region: str, pbf: Path) -> Path:
    OUT.mkdir(exist_ok=True)
    db_path = OUT / f"{region.split('/')[-1]}.sqlite"
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    print(f"[5/5] Schreibe {db_path.name}")
    db = sqlite3.connect(tmp)
    db.execute("CREATE TABLE tiles (y INTEGER NOT NULL, x INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY (y, x)) WITHOUT ROWID")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for (y, x), ways in tiles.items():
        raw = json.dumps({"w": ways}, separators=(",", ":"), ensure_ascii=False).encode()
        c = zlib.compressobj(9, zlib.DEFLATED, -15)  # Raw deflate = Apple's NSData .zlib
        db.execute("INSERT INTO tiles VALUES (?, ?, ?)", (y, x, c.compress(raw) + c.flush()))
    ys = [k[0] for k in tiles]
    xs = [k[1] for k in tiles]
    meta = {"name": region.split("/")[-1], "source": f"Geofabrik {region} (OSM, ODbL)",
            "osm_date": time.strftime("%Y-%m-%d", time.localtime(pbf.stat().st_mtime)),
            "built": time.strftime("%Y-%m-%d"), "tiles": str(len(tiles)),
            "bbox": f"{min(ys) * LAT_STEP:.3f},{min(xs) * LON_STEP:.3f},{(max(ys) + 1) * LAT_STEP:.3f},{(max(xs) + 1) * LON_STEP:.3f}",
            "format": "2"}
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.rename(db_path)
    print(f"      {db_path.stat().st_size // 2**20} MB, {len(tiles)} Kacheln")
    return db_path


def check(db_path: Path, spec: str) -> None:
    """Nearest road + its limit at lat,lon (same distance rule as the app, without heading)."""
    lat, lon, *_ = map(float, spec.split(","))
    y, x = math.floor(lat / LAT_STEP), math.floor(lon / LON_STEP)
    row = sqlite3.connect(db_path).execute("SELECT data FROM tiles WHERE y=? AND x=?", (y, x)).fetchone()
    if not row:
        sys.exit(f"Keine Kachel {y}_{x}")
    ways = json.loads(zlib.decompress(row[0], -15))["w"]
    kx, ky = math.cos(math.radians(lat)) * 111320, 110540
    best = []
    for w in ways:
        c = w["c"]
        pts = [((c[k + 1] / 1e6 - lon) * kx, (c[k] / 1e6 - lat) * ky) for k in range(0, len(c), 2)]
        d = min(seg_dist(pts[k], pts[k + 1]) for k in range(len(pts) - 1))
        best.append((d, w))
    best.sort(key=lambda t: t[0])
    print(f"Kachel {y}_{x}: {len(ways)} Teilstücke")
    for d, w in best[:4]:
        print(f"  {d:5.1f} m  {w['h']:<12} {str(w.get('n')):<28} maxspeed={w.get('m')}{'~' if w.get('e') else ''} fwd={w.get('f')} bwd={w.get('b')} oneway={w.get('o', 0)}")


def seg_dist(a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0 if l2 == 0 else max(0, min(1, -(a[0] * dx + a[1] * dy) / l2))
    return math.hypot(a[0] + t * dx, a[1] + t * dy)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="europe/germany/baden-wuerttemberg", help="Geofabrik path")
    ap.add_argument("--check", metavar="LAT,LON[,COURSE]", help="only query an existing database")
    a = ap.parse_args()
    db = OUT / f"{a.region.split('/')[-1]}.sqlite"
    if a.check:
        check(db, a.check)
    else:
        pbf = download(a.region)
        ways = roads(pbf)
        infer_zones(ways)
        write_db(build_tiles(ways), a.region, pbf)
