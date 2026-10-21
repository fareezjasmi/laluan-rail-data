"""Tests for osm_tracks.py and routing shape-less patterns along track. Invented geometry."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import build_bundle as bb
import polyline_codec
import osm_tracks as ot
from test_build_bundle import write_feed


def osm(ways):
    """ways: list of lists of (lat, lon). Consecutive ways share nodes where coordinates match."""
    ids, elements, next_id = {}, [], 1
    way_elems = []
    for w in ways:
        node_ids = []
        for p in w:
            if p not in ids:
                ids[p] = next_id
                elements.append({"type": "node", "id": next_id, "lat": p[0], "lon": p[1]})
                next_id += 1
            node_ids.append(ids[p])
        way_elems.append({"type": "way", "id": 1000 + len(way_elems), "nodes": node_ids})
    return {"elements": elements + way_elems}


# Track from station A (3.10, 101.60) bending east to B (3.20, 101.65), then to C (3.30, 101.70).
BENT = [(3.1003, 101.6002), (3.15, 101.66), (3.2002, 101.6501)]
B_TO_C = [(3.2002, 101.6501), (3.25, 101.71), (3.3001, 101.7001)]


class RailGraphTest(unittest.TestCase):
    def test_routes_along_track_through_shared_nodes(self):
        g = ot.RailGraph(osm([BENT, B_TO_C]))
        path = g.route((3.10, 101.60), (3.30, 101.70))
        self.assertIsNotNone(path)
        self.assertIn((3.15, 101.66), path)
        self.assertIn((3.25, 101.71), path)
        self.assertEqual(path[0], BENT[0])
        self.assertEqual(path[-1], B_TO_C[-1])

    def test_no_track_near_station(self):
        g = ot.RailGraph(osm([BENT]))
        self.assertIsNone(g.route((3.10, 101.60), (4.5, 102.5)))

    def test_rejects_huge_detour(self):
        # Only connection is a loop far to the east: much longer than the straight line.
        loop = [(3.1003, 101.6002), (3.15, 102.4), (3.2002, 101.6501)]
        g = ot.RailGraph(osm([loop]))
        self.assertIsNone(g.route((3.10, 101.60), (3.20, 101.65)))

    def test_disconnected_pieces(self):
        g = ot.RailGraph(osm([BENT, [(3.29, 101.69), (3.3001, 101.7001)]]))
        self.assertIsNone(g.route((3.10, 101.60), (3.30, 101.70)))

    def test_query_is_bounded_to_malaysia(self):
        self.assertIn("1.2,99.5,7.2,104.6", ot.OVERPASS_QUERY)
        self.assertIn('"railway"="rail"', ot.OVERPASS_QUERY)


class PbfTest(unittest.TestCase):
    """Reads a tiny .osm.pbf written with pyosmium (skipped where pyosmium is not installed)."""

    def test_reads_running_lines_and_skips_sidings(self):
        try:
            import osmium
        except ImportError:
            self.skipTest("pyosmium not installed")
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "rail.osm.pbf")
            writer = osmium.SimpleWriter(path)
            pts = [(3.10, 101.60), (3.15, 101.66), (3.20, 101.65), (3.21, 101.66)]
            for i, (la, lo) in enumerate(pts, start=1):
                writer.add_node(osmium.osm.mutable.Node(id=i, location=osmium.osm.Location(lo, la)))
            writer.add_way(osmium.osm.mutable.Way(id=1, nodes=[1, 2, 3], tags={"railway": "rail"}))
            writer.add_way(osmium.osm.mutable.Way(id=2, nodes=[3, 4], tags={"railway": "rail", "service": "siding"}))
            writer.close()
            g = ot.RailGraph.from_pbf(path)
            self.assertEqual(len(g), 3)  # the siding's end node is not part of the graph
            route = g.route((3.10, 101.60), (3.20, 101.65))
            self.assertIn((3.15, 101.66), route)
        finally:
            shutil.rmtree(tmp)


class TrackGeometryInBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.feed = os.path.join(self.tmp, "feed")
        write_feed(self.feed, overnight=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _station(self, bundle, sid):
        return [s["id"] for s in bundle["stations"]].index(sid)

    def test_shapeless_patterns_follow_track_and_others_are_untouched(self):
        b = bb.BundleBuilder()
        b.add_feed("tst", bb.Feed(self.feed))
        b.add_track_geometry(ot.RailGraph(osm([BENT, B_TO_C])))
        bundle = b.build()
        komuter = next(p for p in bundle["patterns"] if p["service"] == "KOMUTER")
        self.assertNotIn("shape", komuter)  # its geometry comes from the shared legs
        legs = {(leg["a"], leg["b"]): polyline_codec.decode(leg["line"]) for leg in bundle["legs"]}
        a, bb_, c = (self._station(bundle, f"tst:{x}") for x in "ABC")
        self.assertIn((a, bb_), legs)
        self.assertIn((bb_, c), legs)
        self.assertIn((3.15, 101.66), legs[(a, bb_)])  # follows the bend
        # Patterns that already had a feed shape keep it.
        gold = next(p for p in bundle["patterns"] if "EG9101" in p["labels"])
        self.assertEqual(gold["shape"], "tst:s1")
        # A -> E has no track at all: no leg, so the app draws it straight.
        e = self._station(bundle, "tst:E")
        self.assertNotIn((a, e), legs)
        self.assertEqual(b.stats["track_legs_routed"], 2)

    def test_cli_with_osm_file(self):
        osm_path = os.path.join(self.tmp, "osm.json")
        with open(osm_path, "w") as fh:
            json.dump(osm([BENT, B_TO_C]), fh)
        out = os.path.join(self.tmp, "b.json")
        self.assertEqual(bb.main(["--feed", f"tst={self.feed}", "--osm", osm_path, "--out", out]), 0)
        with open(out) as fh:
            data = json.load(fh)
        self.assertEqual(len(data["legs"]), 2)

    def test_cli_tolerates_missing_or_broken_osm(self):
        out = os.path.join(self.tmp, "b.json")
        self.assertEqual(bb.main(["--feed", f"tst={self.feed}", "--osm", "/nope.json", "--out", out]), 0)
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        self.assertEqual(bb.main(["--feed", f"tst={self.feed}", "--osm", bad, "--out", out]), 0)


if __name__ == "__main__":
    unittest.main()
