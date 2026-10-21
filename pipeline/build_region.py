#!/usr/bin/env python3
"""Builds one region's downloadable pack: timetables + real track, ready for the app.

    python3 build_region.py de --work /tmp/work --out dist/
    python3 build_region.py de --work /tmp/work --out dist/ --skip-download   # reuse downloads

Steps: download the region's GTFS feeds and its OpenStreetMap extract, keep one real service day of
trains, route every leg along the track, then write to --out:
    <id>.json.deflate   the bundle (raw DEFLATE; iOS inflates it with NSData.decompressed(using: .zlib))
    <id>.entry.json     its entry for the world index (sizes, credits, a light sketch of its lines)
    <id>.report.txt     what went in, for checking against real timetables

Needs pyosmium for the track (pip install osmium) and, for big countries, the osmium command
(apt install osmium-tool) to cut the extract down to railways first. Without either, trips stay straight.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import date
from typing import List, Optional, Tuple

import build_bundle as bb
import polyline_codec
from regions import Region, region

USER_AGENT = "LaluanRailData/1.0 (+https://github.com/; open timetable packs for a focus app)"

# Services drawn on the globe's sketch of each region (suburban lines would just be noise at that scale).
SKETCH_SERVICES = {"HIGH_SPEED", "INTERCITY", "NIGHT", "REGIONAL_EXPRESS", "REGIONAL", "ETS", "ETS_GOLD",
                   "ETS_PLATINUM", "ETS_EXPRESS", "KOMUTER", "SHUTTLE_TEBRAU", "AIRPORT_RAIL", "RAIL", "MRT", "LRT"}


# ---------------------------------------------------------------- downloads


def download(url: str, dest: str, attempts: int = 3) -> bool:
    tmp = dest + ".part"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1 << 20)
            os.replace(tmp, dest)
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"  download failed ({attempt}/{attempts}) {url}: {e}", file=sys.stderr)
            time.sleep(5 * attempt)
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def is_gtfs_zip(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            names = {os.path.basename(n).lower() for n in z.namelist()}
        return {"stops.txt", "trips.txt", "stop_times.txt"} <= names
    except (zipfile.BadZipFile, OSError):
        return False


def ckan_urls(portal: str, pattern: str, today: date) -> List[str]:
    """Newest .zip resources of a CKAN dataset (this year's and next year's timetable)."""
    urls = []
    for year in (today.year + 1, today.year):
        api = f"{portal}/api/3/action/package_show?id={pattern.format(year=year)}"
        try:
            req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                pkg = json.load(resp)["result"]
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            continue
        zips = [r for r in pkg.get("resources", []) if str(r.get("url", "")).lower().endswith(".zip")]
        zips.sort(key=lambda r: r.get("name") or r.get("url"), reverse=True)  # file names carry the date
        urls += [r["url"] for r in zips[:2]]
    return urls


def expand_years(urls: List[str], today: date) -> List[str]:
    """'.../timetable-{year}-...' -> this year's URL, then next year's."""
    out = []
    for url in urls:
        if "{year}" in url:
            out += [url.format(year=today.year), url.format(year=today.year + 1)]
        else:
            out.append(url)
    return out


def fetch_feeds(reg: Region, work: str, skip: bool) -> List[Tuple[str, str, str]]:
    """[(prefix, path, default_service)] for every feed that downloaded."""
    out = []
    for i, feed in enumerate(reg.feeds):
        dest = os.path.join(work, f"feed-{i}-{feed.prefix}.zip")
        if not (skip and is_gtfs_zip(dest)):
            urls = expand_years(feed.urls, date.today())
            if feed.ckan:
                urls = ckan_urls(feed.ckan[0], feed.ckan[1], date.today()) + urls
            ok = False
            for url in urls:
                print(f"Downloading {reg.id} feed {feed.prefix}: {url}", file=sys.stderr)
                if download(url, dest) and is_gtfs_zip(dest):
                    ok = True
                    break
            if not ok:
                print(f"warning: no working source for {reg.id} feed {feed.prefix}", file=sys.stderr)
                continue
        out.append((feed.prefix, dest, feed.default_service))
    return out


def fetch_track(reg: Region, work: str, skip: bool) -> Optional[str]:
    """A railway-only .osm.pbf for the region, or None."""
    rail = os.path.join(work, "rail.osm.pbf")
    if skip and os.path.exists(rail):
        return rail
    full = os.path.join(work, "region.osm.pbf")
    if not (skip and os.path.exists(full)):
        print(f"Downloading OpenStreetMap extract: {reg.osm_pbf}", file=sys.stderr)
        if not download(reg.osm_pbf, full):
            return None
    if shutil.which("osmium"):
        cmd = ["osmium", "tags-filter", full, "w/railway=rail,light_rail,narrow_gauge", "-o", rail, "--overwrite"]
        if subprocess.run(cmd).returncode == 0:
            os.remove(full)  # several GB for big countries; the filtered file is all we need
            return rail
        print("warning: osmium tags-filter failed; reading the full extract", file=sys.stderr)
    return full


# ---------------------------------------------------------------- sketch for the globe


def sketch_lines(bundle: dict, tolerance_m: float = 900.0) -> List[str]:
    """Each station-to-station leg of the region's main lines once, simplified, precision-4 encoded."""
    stations = bundle["stations"]
    legs = {(l["a"], l["b"]): l["line"] for l in bundle.get("legs", [])}
    shapes = {s["id"]: list(zip(s["points"][0::2], s["points"][1::2])) for s in bundle.get("shapes", [])}
    seen = set()
    out = []
    for p in bundle["patterns"]:
        if p["service"] not in SKETCH_SERVICES:
            continue
        seq = [s[0] for s in p["stops"]]
        if "shape" in p and p["shape"] in shapes:
            key = ("shape", p["shape"])
            if key in seen:
                continue
            seen.add(key)
            pts = shapes[p["shape"]]
            out.append(polyline_codec.encode(bb.simplify(pts, tolerance_m), precision=4))
            continue
        for a, b in zip(seq, seq[1:]):
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            seen.add(key)
            if (a, b) in legs:
                pts = polyline_codec.decode(legs[(a, b)])
            elif (b, a) in legs:
                pts = polyline_codec.decode(legs[(b, a)])
            else:
                pts = [(stations[a]["lat"], stations[a]["lon"]), (stations[b]["lat"], stations[b]["lon"])]
            out.append(polyline_codec.encode(bb.simplify(pts, tolerance_m), precision=4))
    return out


def bbox(bundle: dict) -> List[float]:
    used = {s[0] for p in bundle["patterns"] for s in p["stops"]}
    lats = [bundle["stations"][i]["lat"] for i in used]
    lons = [bundle["stations"][i]["lon"] for i in used]
    return [round(min(lats), 4), round(min(lons), 4), round(max(lats), 4), round(max(lons), 4)]


# ---------------------------------------------------------------- build


def build(reg: Region, feeds: List[Tuple[str, str, str]], track: Optional[str], out_dir: str,
          today: Optional[date] = None) -> dict:
    builder = bb.BundleBuilder(shape_tolerance_m=10.0)
    builder.region = {"id": reg.id, "name": reg.name, "country": reg.country}
    for prefix, path, default_service in feeds:
        print(f"Reading {reg.id} feed {prefix} ({os.path.getsize(path) / 1e6:.0f} MB)", file=sys.stderr)
        builder.add_feed(prefix, bb.Feed(path), classifier=reg.classifier, modes=reg.modes,
                         service_day=reg.service_day, default_service=default_service, today=today)
    if track:
        graph = bb.load_track_graph(osm_pbf=track)
        if graph is not None:
            started = time.time()
            builder.add_track_geometry(graph)
            print(f"Routed track legs in {time.time() - started:.0f}s", file=sys.stderr)
    bundle = builder.build()
    if not bundle["patterns"]:
        raise SystemExit(f"error: {reg.id}: no rail trips found in any feed")

    os.makedirs(out_dir, exist_ok=True)
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw DEFLATE, what Apple's .zlib expects
    packed = compressor.compress(payload) + compressor.flush()
    pack_name = f"{reg.id}.json.deflate"
    with open(os.path.join(out_dir, pack_name), "wb") as fh:
        fh.write(packed)

    services = sorted({p["service"] for p in bundle["patterns"]})
    entry = {
        "id": reg.id,
        "name": reg.name,
        "country": reg.country,
        "group": reg.group,
        "version": bundle["version"],
        "pack": pack_name,
        "packBytes": len(packed),
        "bundleBytes": len(payload),
        "sha256": hashlib.sha256(packed).hexdigest(),
        "bbox": bbox(bundle),
        "stations": len({s[0] for p in bundle["patterns"] for s in p["stops"]}),
        "departures": sum(len(p["departures"]) for p in bundle["patterns"]),
        "services": services,
        "serviceDays": bundle.get("serviceDays", {}),
        "attribution": reg.attribution,
        "lines": sketch_lines(bundle),
    }
    with open(os.path.join(out_dir, f"{reg.id}.entry.json"), "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False, separators=(",", ":"))
    report = bb.write_report(bundle, builder, len(payload), os.path.join(out_dir, f"{reg.id}.report.txt"))
    print(report[:4000])
    print(f"{reg.id}: pack {len(packed) / 1e6:.1f} MB ({len(payload) / 1e6:.1f} MB unpacked), "
          f"{entry['stations']} stations, {entry['departures']} departures, {len(entry['lines'])} sketch lines",
          file=sys.stderr)
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("region", help="region id from regions.py, e.g. de")
    ap.add_argument("--work", default="work", help="download/scratch folder")
    ap.add_argument("--out", default="dist", help="where the pack, index entry and report go")
    ap.add_argument("--skip-download", action="store_true", help="reuse files already in --work")
    ap.add_argument("--no-track", action="store_true", help="skip OpenStreetMap (straight lines)")
    args = ap.parse_args(argv)

    reg = region(args.region)
    work = os.path.join(args.work, reg.id)
    os.makedirs(work, exist_ok=True)
    feeds = fetch_feeds(reg, work, args.skip_download)
    if not feeds:
        print(f"error: {reg.id}: none of its feeds could be downloaded", file=sys.stderr)
        return 1
    track = None if args.no_track else fetch_track(reg, work, args.skip_download)
    build(reg, feeds, track, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
