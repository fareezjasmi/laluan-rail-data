#!/usr/bin/env python3
"""Turn GTFS rail feeds into the compact rail bundle the apps ship with.

Usage:
    python3 build_bundle.py --feed ktmb=raw/ktmb.zip --feed rapidkl=raw/rapid-rail-kl.zip \
        --out ../../ios/Laluan/Resources/rail_bundle.json --report out/report.txt

Each --feed is PREFIX=PATH, where PATH is a GTFS zip file or a folder of GTFS .txt files.
The prefix namespaces ids so several operators can live in one bundle. Two feeds may share a
prefix when they share stop ids (gtfs.de long-distance and regional, for example).

For whole countries (see regions.py / build_region.py):
    --classifier eu        European product names (ICE, TGV, RE, S-Bahn...) instead of Malaysian ones
    --service-day auto     keep only trains running on one real day (a busy midweek day soon)
    --modes rail           heavy rail only (no trams or metros)
    --osm-pbf FILE         route trips along OpenStreetMap track from a .osm.pbf extract

Only the Python standard library is used, so this runs with the python3 that ships
with macOS and in CI without installing anything. Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

import polyline_codec

BUNDLE_FORMAT = 1

# Rail route_type values in GTFS (0 tram/LRT, 1 metro, 2 rail, 12 monorail) plus the
# extended "Hierarchical Vehicle Type" ranges for rail (100-199, 400-499, 900-999).
RAIL_ROUTE_TYPES = {0, 1, 2, 5, 7, 12}


def is_rail_route_type(value: str, modes: str = "all") -> bool:
    """[modes] "all": any rail-guided transport (trains, metros, trams, monorails). "rail": trains only."""
    try:
        t = int(value)
    except (TypeError, ValueError):
        return False
    if modes == "rail":
        return t == 2 or 100 <= t <= 117
    return t in RAIL_ROUTE_TYPES or 100 <= t <= 199 or 400 <= t <= 499 or 900 <= t <= 999


# ---------------------------------------------------------------- reading GTFS


class Feed:
    """Reads GTFS tables from a zip file or a folder."""

    def __init__(self, path: str):
        self.path = path
        self._zip: Optional[zipfile.ZipFile] = None
        self._names: Dict[str, str] = {}
        if os.path.isdir(path):
            for name in os.listdir(path):
                self._names[name.lower()] = os.path.join(path, name)
        else:
            self._zip = zipfile.ZipFile(path)
            for name in self._zip.namelist():
                # Some feeds nest files inside a folder in the zip.
                self._names[os.path.basename(name).lower()] = name

    def has(self, table: str) -> bool:
        return f"{table}.txt" in self._names

    def rows(self, table: str) -> Iterator[Dict[str, str]]:
        """Streams a table row by row, so country-sized stop_times never sit in memory whole."""
        key = f"{table}.txt"
        if key not in self._names:
            return
        if self._zip is not None:
            binary = self._zip.open(self._names[key])
        else:
            binary = open(self._names[key], "rb")
        with binary, io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="") as text:
            reader = csv.reader(text)
            header = next(reader, None)
            if header is None:
                return
            names = [h.strip() for h in header]
            width = len(names)
            for row in reader:
                if not row:
                    continue
                if len(row) < width:
                    row = row + [""] * (width - len(row))
                yield {names[i]: row[i].strip() for i in range(width)}


# ---------------------------------------------------------------- service days


def _ymd(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError:
        return None


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class Calendar:
    """Which service_ids run on which dates (calendar.txt plus calendar_dates.txt)."""

    def __init__(self, feed: Feed):
        self.weekly: Dict[str, Tuple[date, date, List[bool]]] = {}
        for r in feed.rows("calendar"):
            start, end = _ymd(r.get("start_date", "")), _ymd(r.get("end_date", ""))
            if start and end:
                self.weekly[r["service_id"]] = (start, end, [r.get(d, "0") == "1" for d in WEEKDAYS])
        self.added: Dict[date, Set[str]] = defaultdict(set)
        self.removed: Dict[date, Set[str]] = defaultdict(set)
        for r in feed.rows("calendar_dates"):
            d = _ymd(r.get("date", ""))
            if d is None:
                continue
            (self.added if r.get("exception_type") == "1" else self.removed)[d].add(r.get("service_id", ""))

    @property
    def empty(self) -> bool:
        return not self.weekly and not self.added

    def active(self, day: date) -> Set[str]:
        out = {sid for sid, (start, end, days) in self.weekly.items() if start <= day <= end and days[day.weekday()]}
        out |= self.added.get(day, set())
        out -= self.removed.get(day, set())
        return out

    def first_date(self) -> Optional[date]:
        dates = [v[0] for v in self.weekly.values()] + list(self.added.keys())
        return min(dates) if dates else None


def choose_service_day(calendar: Calendar, trips_per_service: Counter, today: date) -> Optional[date]:
    """A busy, ordinary day: the Tuesday-Thursday with most trains in the next two weeks.

    Falls back to any day in that window, then to the two weeks after the feed starts (for feeds
    published ahead of their timetable change).
    """
    def score(day: date) -> int:
        return sum(trips_per_service[s] for s in calendar.active(day))

    for start in (today, calendar.first_date()):
        if start is None:
            continue
        window = [start + timedelta(days=i) for i in range(1, 15)]
        midweek = [d for d in window if d.weekday() in (1, 2, 3)]
        for candidates in (midweek, window):
            best = max(candidates, key=lambda d: (score(d), -d.toordinal()), default=None)
            if best is not None and score(best) > 0:
                return best
    return None


def parse_gtfs_time(value: str) -> Optional[int]:
    """'25:10:00' -> seconds after midnight of the service day (may exceed 24h)."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    if m > 59 or s > 59 or h < 0:
        return None
    return h * 3600 + m * 60 + s


# ---------------------------------------------------------------- classification

# Service codes shared with the Kotlin core (ServiceType.kt). Keep the two in sync.
SERVICE_CODES = [
    "KOMUTER", "ETS", "ETS_GOLD", "ETS_PLATINUM", "ETS_EXPRESS", "INTERCITY",
    "SHUTTLE_TEBRAU", "LRT", "MRT", "MONORAIL", "BRT", "AIRPORT_RAIL", "RAIL",
    # Generic types for the rest of the world (European feeds first).
    "HIGH_SPEED", "NIGHT", "REGIONAL_EXPRESS", "REGIONAL", "SUBURBAN",
]


def classify_route(route: Dict[str, str]) -> str:
    rid = route.get("route_id", "").upper()
    short = route.get("route_short_name", "").upper()
    long = route.get("route_long_name", "").upper()
    desc = route.get("route_desc", "").upper()
    text = " ".join([rid, short, long, desc])

    if "SHUTTLE TEBRAU" in text or rid == "ST":
        return "SHUTTLE_TEBRAU"
    if rid == "ETS" or short == "ETS" or "ELECTRIC TRAIN SERVICE" in text or text.startswith("ETS"):
        return "ETS"
    if "KOMUTER" in text or "KTM KOMUTER" in text:
        return "KOMUTER"
    if "INTERCITY" in text or "EKSPRES" in text or "SENANDUNG" in text:
        return "INTERCITY"
    # KTMB names its Komuter lines "KTM <from> - <to>" without the word Komuter.
    if long.startswith("KTM "):
        return "KOMUTER"
    # RapidKL names lines without saying LRT or MRT.
    if any(k in long for k in ("KELANA JAYA", "AMPANG", "SRI PETALING", "SHAH ALAM")):
        return "LRT"
    if any(k in long for k in ("KAJANG", "PUTRAJAYA", "CIRCLE LINE")):
        return "MRT"
    if "MONORAIL" in text or short == "MR":
        return "MONORAIL"
    if "MRT" in text:
        return "MRT"
    if "LRT" in text:
        return "LRT"
    if "BRT" in text:
        return "BRT"
    if "ERL" in text or "KLIA" in text or "AIRPORT" in text:
        return "AIRPORT_RAIL"
    return "RAIL"


def refine_ets(service: str, trip: Dict[str, str]) -> str:
    """ETS trips are often numbered EG..., EP..., EE... — use that to tell Gold/Platinum/Express.

    This is a heuristic until we see the real feed; anything unrecognised stays plain ETS.
    """
    if service != "ETS":
        return service
    label = (trip.get("trip_short_name") or "").upper() + " " + (trip.get("trip_headsign") or "").upper()
    label = label.strip()
    if "PLATINUM" in label or label.startswith("EP"):
        return "ETS_PLATINUM"
    if "GOLD" in label or label.startswith("EG"):
        return "ETS_GOLD"
    if "EXPRESS" in label or label.startswith("EE"):
        return "ETS_EXPRESS"
    return "ETS"


# European product names (the letters on the train's destination board) -> service code.
EU_PRODUCTS = {
    "HIGH_SPEED": {"ICE", "TGV", "INOUI", "OUIGO", "LYRIA", "EUROSTAR", "EST", "THALYS", "THA", "AVE", "AVLO",
                   "IRYO", "AV", "EUROMED", "FR", "FA", "RJ", "RJX", "RAILJET", "ICE-SPRINTER", "FRECCIAROSSA"},
    "NIGHT": {"NJ", "EN", "NIGHTJET", "TRENHOTEL", "EUROPEAN SLEEPER", "INTERCITÉS DE NUIT", "INTERCITES DE NUIT"},
    "INTERCITY": {"IC", "EC", "ECE", "ICD", "ICN", "FLX", "D", "ALVIA", "INTERCITY", "INTERCITÉS", "INTERCITES",
                  "LD", "IC DIRECT", "INTERCITY DIRECT", "EUROCITY", "WB"},
    "REGIONAL_EXPRESS": {"RE", "IRE", "RX", "IR", "MEX", "REX", "MD", "TER EXPRESS", "INTERREGIO", "FEX", "ALS"},
    "REGIONAL": {"RB", "R", "TER", "REGIO", "REG", "REGIONAL", "L", "P", "SPR", "SPRINTER", "RL", "CR", "PE", "ST",
                 "REGIONAL EXPRESS TRAIN"},
    "SUBURBAN": {"S", "RER", "TRANSILIEN", "CERCANÍAS", "CERCANIAS", "RODALIES", "S-BAHN", "SL", "SN", "TRAMTRAIN"},
}
_EU_ORDER = ["NIGHT", "HIGH_SPEED", "INTERCITY", "REGIONAL_EXPRESS", "SUBURBAN", "REGIONAL"]
# GTFS extended route types (Hierarchical Vehicle Types) that already say what the train is.
EU_ROUTE_TYPES = {101: "HIGH_SPEED", 102: "INTERCITY", 103: "REGIONAL_EXPRESS", 105: "NIGHT", 106: "REGIONAL",
                  109: "SUBURBAN", 116: "REGIONAL", 117: "REGIONAL"}


def _product_token(text: str) -> str:
    """'ICE 578' -> 'ICE', 'RE5' -> 'RE', 'S 3' -> 'S', 'C-1' -> 'C'."""
    text = text.strip().upper()
    token = ""
    for ch in text:
        if ch.isalpha() or ch in "ÉÍ-" and token:
            token += ch
        else:
            break
    return token.rstrip("-")


def classify_route_eu(route: Dict[str, str], agency: str = "", default: str = "") -> Tuple[str, str]:
    """(service code, badge) for a European rail route.

    The badge is what riders see on the train: 'ICE', 'TGV INOUI', 'RE 5', 'S3'. Names beat route_type,
    because many feeds mark every train as plain rail (2).
    """
    short = route.get("route_short_name", "").strip()
    long = route.get("route_long_name", "").strip()
    desc = route.get("route_desc", "").strip()
    texts = [t.upper() for t in (short, desc, long, agency) if t]
    found = ""
    for text in texts:
        for code in _EU_ORDER:
            names = EU_PRODUCTS[code]
            if _product_token(text) in names or text in names or any(
                    len(n) > 3 and n in text for n in names):
                found = code
                break
        if found:
            break
    if not found:
        try:
            found = EU_ROUTE_TYPES.get(int(route.get("route_type", "")), "")
        except ValueError:
            found = ""
    service = found or default or "REGIONAL"
    # Only real product names make a badge ("RE 5", "S3", "TGV"); bare line numbers like "001A" do not.
    if short and len(short) <= 9 and short[0].isalpha():
        badge = short
    else:
        token = _product_token(desc) or _product_token(short)
        badge = token if 0 < len(token) <= 9 else ""
    return service, badge


# ---------------------------------------------------------------- geometry


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(h)))


def _perp_distance_m(p, a, b) -> float:
    # Equirectangular projection is fine at the scale of a simplification tolerance.
    lat0 = math.radians((a[0] + b[0]) / 2)

    def xy(q):
        return (math.radians(q[1]) * math.cos(lat0) * 6371000.0, math.radians(q[0]) * 6371000.0)

    px, py = xy(p)
    ax, ay = xy(a)
    bx, by = xy(b)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points: List[Tuple[float, float]], tolerance_m: float) -> List[Tuple[float, float]]:
    """Douglas-Peucker, iterative so long shapes don't hit the recursion limit."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        max_d, idx = 0.0, -1
        for i in range(start + 1, end):
            d = _perp_distance_m(points[i], points[start], points[end])
            if d > max_d:
                max_d, idx = d, i
        if idx != -1 and max_d > tolerance_m:
            keep[idx] = True
            stack.append((start, idx))
            stack.append((idx, end))
    return [p for p, k in zip(points, keep) if k]


# ---------------------------------------------------------------- building


class BundleBuilder:
    def __init__(self, shape_tolerance_m: float = 15.0):
        self.shape_tolerance_m = shape_tolerance_m
        self.stations: List[dict] = []
        self.station_index: Dict[str, int] = {}
        self.routes: List[dict] = []
        self.shapes: List[dict] = []
        self.patterns: Dict[tuple, dict] = {}
        self.sources: List[dict] = []
        self.warnings: List[str] = []
        self.stats = Counter()
        self.service_days: Dict[str, str] = {}
        # Track between consecutive stations, shared by every pattern that runs that way: (a, b) -> points.
        self.legs: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
        self.region: Optional[dict] = None

    # ---- per feed

    def add_feed(self, prefix: str, feed: Feed, classifier: str = "my", modes: str = "all",
                 service_day=None, default_service: str = "", today: Optional[date] = None) -> None:
        """Adds one GTFS feed.

        [classifier] "my" (Malaysian names) or "eu" (European products). [modes] "all" or "rail".
        [service_day] None keeps every trip; a date, or "auto" for a busy midweek day soon, keeps only
        the trains running that day, so a country's 30-day feed becomes one real day's timetable.
        """
        agencies = {a.get("agency_id", ""): a.get("agency_name", "") for a in feed.rows("agency")}
        agency_names = list(agencies.values())
        self.sources.append({
            "prefix": prefix,
            "agency": ", ".join(n for n in dict.fromkeys(agency_names) if n)[:200] or prefix,
            "file": os.path.basename(feed.path),
        })

        routes = {}
        known_routes = {r["id"] for r in self.routes}
        for r in feed.rows("routes"):
            if not is_rail_route_type(r.get("route_type", ""), modes):
                self.stats[f"{prefix}.routes_skipped_not_rail"] += 1
                continue
            if classifier == "eu":
                agency = agencies.get(r.get("agency_id", ""), "") if len(agencies) > 1 else ""
                service, badge = classify_route_eu(r, agency, default_service)
            else:
                service, badge = classify_route(r), ""
            rid = f"{prefix}:{r['route_id']}"
            routes[r["route_id"]] = service
            if rid in known_routes:
                continue  # a second feed with the same prefix and route (shared ids)
            known_routes.add(rid)
            entry = {
                "id": rid,
                "name": r.get("route_long_name") or r.get("route_short_name") or r["route_id"],
                "shortName": r.get("route_short_name", ""),
                "service": service,
                "color": normalise_color(r.get("route_color", "")),
            }
            if badge:
                entry["badge"] = badge
            self.routes.append(entry)
        if not routes:
            self.warnings.append(f"{prefix}: no rail routes found")
            return

        # Stops: collapse platforms onto their parent station.
        raw_stops = {s["stop_id"]: s for s in feed.rows("stops")}

        def station_key(stop_id: str) -> Optional[str]:
            s = raw_stops.get(stop_id)
            if s is None:
                return None
            parent = s.get("parent_station", "")
            if parent and parent in raw_stops:
                return parent
            return stop_id

        trips = {}
        for t in feed.rows("trips"):
            if t.get("route_id") in routes:
                trips[t["trip_id"]] = t

        if service_day is not None:
            calendar = Calendar(feed)
            if calendar.empty:
                self.warnings.append(f"{prefix}: no calendar, keeping every trip")
            else:
                per_service = Counter(t.get("service_id", "") for t in trips.values())
                day = choose_service_day(calendar, per_service, today or date.today()) \
                    if service_day == "auto" else service_day
                if day is None:
                    self.warnings.append(f"{prefix}: no service day found in the feed, keeping every trip")
                else:
                    active = calendar.active(day)
                    before = len(trips)
                    trips = {tid: t for tid, t in trips.items() if t.get("service_id", "") in active}
                    self.service_days[prefix] = day.isoformat()
                    self.stats[f"{prefix}.trips_not_on_service_day"] += before - len(trips)

        # Headway-based services (frequencies.txt): trip_id -> list of departure times from its first stop.
        freq: Dict[str, List[int]] = defaultdict(list)
        for f in feed.rows("frequencies"):
            tid = f.get("trip_id")
            if tid not in trips:
                continue
            start, end = parse_gtfs_time(f.get("start_time", "")), parse_gtfs_time(f.get("end_time", ""))
            try:
                headway = int(f.get("headway_secs", "0"))
            except ValueError:
                headway = 0
            if start is None or end is None or headway <= 0:
                continue
            t = start
            while t < end:
                freq[tid].append(t)
                t += headway

        # Group stop_times by trip.
        by_trip: Dict[str, List[Tuple[int, str, Optional[int], Optional[int]]]] = defaultdict(list)
        for st in feed.rows("stop_times"):
            tid = st.get("trip_id")
            if tid not in trips:
                continue
            try:
                seq = int(st.get("stop_sequence", "0"))
            except ValueError:
                seq = 0
            by_trip[tid].append((seq, st.get("stop_id", ""),
                                 parse_gtfs_time(st.get("arrival_time", "")),
                                 parse_gtfs_time(st.get("departure_time", ""))))

        # Shapes used by rail trips.
        used_shapes = {t.get("shape_id") for t in trips.values() if t.get("shape_id")}
        shape_ids: Dict[str, str] = {}
        if used_shapes and feed.has("shapes"):
            pts: Dict[str, List[Tuple[int, float, float]]] = defaultdict(list)
            for sp in feed.rows("shapes"):
                sid = sp.get("shape_id")
                if sid not in used_shapes:
                    continue
                try:
                    pts[sid].append((int(sp.get("shape_pt_sequence", "0")),
                                     float(sp["shape_pt_lat"]), float(sp["shape_pt_lon"])))
                except (KeyError, ValueError):
                    continue
            for sid, lst in pts.items():
                lst.sort()
                simplified = simplify([(la, lo) for _, la, lo in lst], self.shape_tolerance_m)
                if len(simplified) < 2:
                    continue
                full_id = f"{prefix}:{sid}"
                shape_ids[sid] = full_id
                flat: List[float] = []
                for la, lo in simplified:
                    flat.extend([round(la, 6), round(lo, 6)])
                self.shapes.append({"id": full_id, "points": flat})
                self.stats["shape_points_in"] += len(lst)
                self.stats["shape_points_out"] += len(simplified)

        for tid, rows in by_trip.items():
            rows.sort()
            trip = trips[tid]
            stops: List[Tuple[int, int, int]] = []
            ok = True
            for _, stop_id, arr, dep in rows:
                key = station_key(stop_id)
                if key is None:
                    ok = False
                    break
                if arr is None and dep is None:
                    # Untimed stop: skip it rather than inventing a time.
                    self.stats["untimed_stops_skipped"] += 1
                    continue
                arr = arr if arr is not None else dep
                dep = dep if dep is not None else arr
                idx = self._station(prefix, key, raw_stops[key])
                if stops and stops[-1][0] == idx:
                    # Same station twice in a row (platform change): merge.
                    stops[-1] = (idx, stops[-1][1], max(stops[-1][2], dep))
                    continue
                stops.append((idx, arr, dep))
            if not ok:
                self.stats["trips_skipped_unknown_stop"] += 1
                continue
            if len(stops) < 2:
                self.stats["trips_skipped_too_short"] += 1
                continue
            if any(stops[i + 1][1] < stops[i][2] for i in range(len(stops) - 1)):
                self.stats["trips_skipped_time_goes_backwards"] += 1
                continue
            service = refine_ets(routes[trip["route_id"]], trip)
            self._add_pattern(prefix, trip, service, shape_ids.get(trip.get("shape_id", "")), stops, freq.get(tid))
            self.stats["trips_in"] += 1

    def _station(self, prefix: str, key: str, stop: Dict[str, str]) -> int:
        sid = f"{prefix}:{key}"
        if sid in self.station_index:
            return self.station_index[sid]
        try:
            lat = round(float(stop.get("stop_lat", "")), 6)
            lon = round(float(stop.get("stop_lon", "")), 6)
        except ValueError:
            lat, lon = 0.0, 0.0
            self.warnings.append(f"station {sid} has no coordinates")
        self.station_index[sid] = len(self.stations)
        self.stations.append({
            "id": sid,
            "code": stop.get("stop_code", "") or key,
            "name": tidy_name(stop.get("stop_name", "") or key),
            "lat": lat,
            "lon": lon,
        })
        return self.station_index[sid]

    def _add_pattern(self, prefix, trip, service, shape_id, stops, frequency_departures=None) -> None:
        first_dep = stops[0][2]
        rel = tuple((i, a - first_dep, d - first_dep) for i, a, d in stops)
        route_id = f"{prefix}:{trip['route_id']}"
        key = (route_id, service, trip.get("trip_headsign", ""), shape_id, rel)
        pattern = self.patterns.get(key)
        if pattern is None:
            pattern = {
                "id": f"{prefix}:p{len(self.patterns) + 1}",
                "route": route_id,
                "service": service,
                "headsign": trip.get("trip_headsign", ""),
                "shape": shape_id,
                "stops": [list(s) for s in rel],
                "departures": [],
                "labels": [],
            }
            self.patterns[key] = pattern
        label = train_label(trip)
        departures = frequency_departures or [first_dep]
        if frequency_departures:
            label = ""  # one trip_id stands for many trains; its id is not a train number
        seen = set(pattern["departures"])
        for d in departures:
            dep = d % 86400
            if dep not in seen:
                seen.add(dep)
                pattern["departures"].append(dep)
                pattern["labels"].append(label)

    # ---- real track geometry for patterns without shapes

    def add_track_geometry(self, graph) -> None:
        """Routes every station-to-station leg of shape-less patterns along OpenStreetMap track.

        Legs are stored once and shared: in a country, thousands of patterns run over the same
        few thousand legs, so this is far smaller than a shape per pattern. The app joins a
        pattern's legs back together (legs that could not be routed are drawn straight).
        """
        tried: Set[Tuple[int, int]] = set()
        for p in self.patterns.values():
            if p["shape"] is not None:
                continue
            seq = [s[0] for s in p["stops"]]
            for a, b in zip(seq, seq[1:]):
                if (a, b) in tried:
                    continue
                tried.add((a, b))
                pa = (self.stations[a]["lat"], self.stations[a]["lon"])
                pb = (self.stations[b]["lat"], self.stations[b]["lon"])
                path = graph.route(pa, pb)
                if path and len(path) >= 2:
                    self.legs[(a, b)] = simplify(path, self.shape_tolerance_m)
                    self.stats["track_legs_routed"] += 1
                else:
                    self.stats["track_legs_straight"] += 1

    # ---- output

    def build(self) -> dict:
        patterns = list(self.patterns.values())
        for p in patterns:
            order = sorted(range(len(p["departures"])), key=lambda i: p["departures"][i])
            p["departures"] = [p["departures"][i] for i in order]
            p["labels"] = [p["labels"][i] for i in order]
            if not any(p["labels"]):
                p["labels"] = []
            if p["shape"] is None:
                del p["shape"]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        bundle = {
            "format": BUNDLE_FORMAT,
            "version": now.strftime("%Y%m%d%H%M"),
            "generatedAt": now.isoformat(),
            "sources": self.sources,
            "stations": self.stations,
            "routes": self.routes,
            "shapes": self.shapes,
            "legs": [{"a": a, "b": b, "line": polyline_codec.encode(pts)}
                     for (a, b), pts in sorted(self.legs.items())],
            "patterns": patterns,
        }
        if self.service_days:
            bundle["serviceDays"] = self.service_days
        if self.region:
            bundle["region"] = self.region
        return bundle


# Short words that stay upper case when tidying ALL-CAPS station names.
ACRONYMS = {"KL", "KLCC", "KLIA", "KLIA2", "KTM", "KTMB", "LRT", "MRT", "BRT", "ERL", "CIQ", "JB", "PJ",
            "UM", "UKM", "UPM", "UTM", "USJ", "TBS", "IOI", "MRR2", "SS", "KTL", "SPL"}


def train_label(trip: Dict[str, str]) -> str:
    """Train number to show riders: trip_short_name, else a short numeric trip_id (KTMB uses train numbers)."""
    short = trip.get("trip_short_name", "").strip()
    if short:
        return short
    tid = trip.get("trip_id", "").strip()
    return tid if tid.isdigit() and len(tid) <= 5 else ""


def tidy_name(name: str) -> str:
    """'HAT YAI' -> 'Hat Yai', 'KL SENTRAL' -> 'KL Sentral'. Mixed-case names are left alone."""
    name = " ".join(name.split())
    if not name.isupper():
        return name

    def fix(word: str) -> str:
        core = word.strip("()/-,.")
        if core in ACRONYMS or any(c.isdigit() for c in core):
            return word
        return "/".join("-".join(p.capitalize() for p in part.split("-")) for part in word.split("/"))

    return " ".join(fix(w) for w in name.split(" "))


def normalise_color(value: str) -> str:
    value = value.strip().lstrip("#")
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value.upper()
    return ""


# ---------------------------------------------------------------- report


def write_report(bundle: dict, builder: BundleBuilder, out_size: int, path: str) -> str:
    lines = []
    lines.append(f"Rail bundle report  {bundle['generatedAt']}")
    lines.append(f"Output size: {out_size / 1024:.0f} KB")
    lines.append("")
    for s in bundle["sources"]:
        lines.append(f"Source {s['prefix']}: {s['agency']} ({s['file']})")
    lines.append("")
    lines.append(f"Stations: {len(bundle['stations'])}")
    lines.append(f"Routes:   {len(bundle['routes'])}")
    lines.append(f"Shapes:   {len(bundle['shapes'])}")
    lines.append(f"Patterns: {len(bundle['patterns'])} (from {builder.stats['trips_in']} trips)")
    lines.append("")
    lines.append("Routes by service type:")
    for r in sorted(bundle["routes"], key=lambda r: (r["service"], r["id"])):
        lines.append(f"  {r['service']:<15} {r['id']:<28} {r['name']}  [short='{r['shortName']}']")
    lines.append("")
    svc = Counter()
    trips = Counter()
    for p in bundle["patterns"]:
        svc[p["service"]] += 1
        trips[p["service"]] += len(p["departures"])
    lines.append("Patterns / departures by service:")
    for k in sorted(svc):
        lines.append(f"  {k:<15} {svc[k]:>5} patterns  {trips[k]:>6} departures")
    lines.append("")
    lines.append("Pipeline counters:")
    for k in sorted(builder.stats):
        lines.append(f"  {k}: {builder.stats[k]}")
    if builder.warnings:
        lines.append("")
        lines.append(f"Warnings ({len(builder.warnings)}):")
        for w in builder.warnings[:50]:
            lines.append(f"  {w}")
    lines.append("")
    lines.append("Longest end-to-end pattern per service (sample for checking against timetables):")
    names = bundle["stations"]
    best: Dict[str, dict] = {}
    for p in bundle["patterns"]:
        dur = p["stops"][-1][1]
        if p["service"] not in best or dur > best[p["service"]]["stops"][-1][1]:
            best[p["service"]] = p
    for k in sorted(best):
        p = best[k]
        a, b = names[p["stops"][0][0]]["name"], names[p["stops"][-1][0]]["name"]
        dep = p["departures"][0] if p["departures"] else 0
        label = p["labels"][0] if p["labels"] else ""
        lines.append(f"  {k:<15} {a} -> {b}: {fmt_dur(p['stops'][-1][1])}, {len(p['stops'])} stops, "
                     f"dep {fmt_clock(dep)} {label}".rstrip())
    lines.append("")
    lines.append("Sample train labels / headsigns (first 25 ETS-family patterns):")
    n = 0
    for p in bundle["patterns"]:
        if p["service"].startswith("ETS"):
            lines.append(f"  {p['service']:<13} headsign='{p['headsign']}' labels={p['labels'][:4]}")
            n += 1
            if n >= 25:
                break
    text = "\n".join(lines) + "\n"
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


def fmt_dur(sec: int) -> str:
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def fmt_clock(sec: int) -> str:
    sec %= 86400
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


# ---------------------------------------------------------------- main


def load_track_graph(osm_json: str = "", osm_pbf: str = ""):
    """The OpenStreetMap track graph from either input, or None (trips then stay straight)."""
    from osm_tracks import RailGraph
    for path, loader in ((osm_pbf, RailGraph.from_pbf), (osm_json, RailGraph.from_file)):
        if not path:
            continue
        if not os.path.exists(path):
            print(f"warning: {path} not found; trips without shapes stay straight", file=sys.stderr)
            continue
        try:
            graph = loader(path)
            print(f"OpenStreetMap track graph: {len(graph)} nodes", file=sys.stderr)
            return graph
        except (ValueError, OSError, ImportError) as e:
            print(f"warning: could not use OpenStreetMap data ({e}); trips without shapes stay straight", file=sys.stderr)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", action="append", required=True, help="PREFIX=PATH to a GTFS zip or folder")
    ap.add_argument("--out", required=True, help="output bundle JSON path")
    ap.add_argument("--report", default="", help="optional text report path")
    ap.add_argument("--shape-tolerance", type=float, default=15.0, help="shape simplification in metres")
    ap.add_argument("--osm", default="", help="optional OpenStreetMap railway JSON (Overpass) to route shape-less trips along real track")
    ap.add_argument("--osm-pbf", default="", help="optional OpenStreetMap .osm.pbf extract (needs pyosmium) instead of --osm")
    ap.add_argument("--classifier", choices=["my", "eu"], default="my", help="how to name train types")
    ap.add_argument("--modes", choices=["all", "rail"], default="all", help="'rail' keeps trains only (no trams/metros)")
    ap.add_argument("--service-day", default="", help="'auto' or YYYY-MM-DD: keep only that day's trains")
    args = ap.parse_args(argv)

    service_day = None
    if args.service_day == "auto":
        service_day = "auto"
    elif args.service_day:
        service_day = _ymd(args.service_day.replace("-", ""))
        if service_day is None:
            ap.error("--service-day must be 'auto' or YYYY-MM-DD")

    builder = BundleBuilder(shape_tolerance_m=args.shape_tolerance)
    for spec in args.feed:
        if "=" not in spec:
            ap.error(f"--feed must be PREFIX=PATH, got {spec!r}")
        prefix, path = spec.split("=", 1)
        if not os.path.exists(path):
            print(f"warning: feed {prefix} not found at {path}, skipping", file=sys.stderr)
            continue
        builder.add_feed(prefix, Feed(path), classifier=args.classifier, modes=args.modes, service_day=service_day)

    graph = load_track_graph(args.osm, args.osm_pbf)
    if graph is not None:
        builder.add_track_geometry(graph)

    bundle = builder.build()
    if not bundle["patterns"]:
        print("error: no rail trips found in any feed", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(payload)
    report = write_report(bundle, builder, len(payload.encode("utf-8")), args.report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
