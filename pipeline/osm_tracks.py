"""Real railway track geometry from OpenStreetMap, for feeds that ship no shapes (KTMB).

The Overpass query below returns every mainline railway way in Peninsular Malaysia (plus the
lines into southern Thailand and Woodlands). We build a graph from it and route each leg of a
trip, station to station, along the track. Legs that can't be routed sensibly fall back to a
straight line, so the result is never worse than before.

Data © OpenStreetMap contributors, ODbL. Standard library only.
"""
from __future__ import annotations

import heapq
import json
import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# south, west, north, east
BBOX = (1.20, 99.50, 7.20, 104.60)

OVERPASS_QUERY = (
    "[out:json][timeout:300];"
    f'(way["railway"="rail"]["service"!~"yard|siding|spur|crossover"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]}););'
    "(._;>;);out body qt;"
)

LatLon = Tuple[float, float]

# Railway kinds trains run on, and service tracks they don't run along in service.
RAIL_KINDS = {"rail", "light_rail", "narrow_gauge"}
SKIP_SERVICE = {"yard", "siding", "spur", "crossover"}

_R = 6_371_000.0


def haversine_m(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _R * math.asin(min(1.0, math.sqrt(h)))


class RailGraph:
    """Undirected graph of OSM railway nodes, weighted by distance in metres."""

    CELL = 0.01  # degrees, about 1.1 km

    def __init__(self, osm: Optional[dict] = None, coords: Optional[Dict[int, LatLon]] = None,
                 ways: Optional[List[List[int]]] = None):
        """From Overpass JSON ([osm]), or directly from node [coords] and [ways] (node id lists)."""
        coords = dict(coords or {})
        ways = list(ways or [])
        for el in (osm or {}).get("elements", []):
            if el.get("type") == "node" and "lat" in el:
                coords[el["id"]] = (el["lat"], el["lon"])
            elif el.get("type") == "way" and el.get("nodes"):
                ways.append(el["nodes"])
        self.coords = coords
        self.adj: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
        for nodes in ways:
            for a, b in zip(nodes, nodes[1:]):
                if a in coords and b in coords and a != b:
                    d = haversine_m(coords[a], coords[b])
                    self.adj[a].append((b, d))
                    self.adj[b].append((a, d))
        self.grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for n in self.adj:
            self.grid[self._cell(coords[n])].append(n)

    @classmethod
    def from_file(cls, path: str) -> "RailGraph":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    @classmethod
    def from_pbf(cls, path: str) -> "RailGraph":
        """Reads railway ways straight from an .osm.pbf extract (Geofabrik), with pyosmium.

        Same graph as from Overpass JSON: running lines only, no yards or sidings. For a country,
        filter the extract first (osmium tags-filter ... w/railway) so this stays quick.
        """
        import osmium  # pip install osmium; only needed for country builds

        coords: Dict[int, LatLon] = {}
        ways: List[List[int]] = []

        class Handler(osmium.SimpleHandler):
            def way(self, w):
                tags = w.tags
                if tags.get("railway") not in RAIL_KINDS or tags.get("service") in SKIP_SERVICE:
                    return
                if tags.get("usage") == "tourism":
                    return
                ids = []
                for n in w.nodes:
                    if not n.location.valid():
                        continue
                    coords[n.ref] = (n.location.lat, n.location.lon)
                    ids.append(n.ref)
                if len(ids) >= 2:
                    ways.append(ids)

        Handler().apply_file(path, locations=True)
        return cls(coords=coords, ways=ways)

    def __len__(self) -> int:
        return len(self.adj)

    def _cell(self, p: LatLon) -> Tuple[int, int]:
        return (int(math.floor(p[0] / self.CELL)), int(math.floor(p[1] / self.CELL)))

    def nearest(self, p: LatLon, max_m: float = 800.0) -> Optional[int]:
        cx, cy = self._cell(p)
        reach = int(math.ceil(max_m / 1100.0))
        best, best_d = None, max_m
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for n in self.grid.get((cx + dx, cy + dy), ()):
                    d = haversine_m(p, self.coords[n])
                    if d < best_d:
                        best, best_d = n, d
        return best

    def route(self, a: LatLon, b: LatLon, max_detour: float = 2.2) -> Optional[List[LatLon]]:
        """Track path from near [a] to near [b], or None if there isn't a believable one.

        A* over the graph; gives up once the path would be longer than [max_detour] times the
        straight-line distance (plus slack), which also stops wrong-way routings via far junctions.
        """
        start, goal = self.nearest(a), self.nearest(b)
        if start is None or goal is None:
            return None
        if start == goal:
            return [self.coords[start]]
        target = self.coords[goal]
        limit = haversine_m(a, b) * max_detour + 3000.0
        dist = {start: 0.0}
        prev: Dict[int, int] = {}
        heap = [(haversine_m(self.coords[start], target), 0.0, start)]
        while heap:
            _, d, n = heapq.heappop(heap)
            if n == goal:
                break
            if d > dist.get(n, math.inf) or d > limit:
                continue
            for m, w in self.adj[n]:
                nd = d + w
                if nd < dist.get(m, math.inf) and nd <= limit:
                    dist[m] = nd
                    prev[m] = n
                    heapq.heappush(heap, (nd + haversine_m(self.coords[m], target), nd, m))
        if goal not in dist:
            return None
        path = [goal]
        while path[-1] != start:
            path.append(prev[path[-1]])
        path.reverse()
        return [self.coords[n] for n in path]
