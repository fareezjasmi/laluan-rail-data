"""Tests for build_bundle.py. Run: python3 -m unittest discover -s data/pipeline

The fixture is a small invented network (station names A..E) used only for tests.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import zipfile

import build_bundle as bb

AGENCY = "agency_id,agency_name,agency_url,agency_timezone\nT,Test Rail,https://example.com,Asia/Kuala_Lumpur\n"

ROUTES = (
    "route_id,agency_id,route_short_name,route_long_name,route_type,route_color\n"
    "ETS,T,ETS,Electric Train Service,2,0055aa\n"
    "KC,T,KC,Laluan Komuter Test,0,\n"
    "BUS1,T,B1,Feeder bus,3,\n"
)

TRIPS = (
    "route_id,service_id,trip_id,trip_headsign,trip_short_name,shape_id\n"
    "ETS,WK,t1,E Town,EP9001,s1\n"
    "ETS,WK,t2,E Town,EG9101,s1\n"
    "KC,WK,k1,C Town,,\n"
    "KC,WK,k2,C Town,,\n"
    "KC,WE,k3,C Town,,\n"
    "BUS1,WK,b1,Somewhere,,\n"
)

# A has two platforms (A1, A2) under parent station A.
STOPS = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
    "A,AAA,ALPHA CENTRAL,3.1000,101.6000,1,\n"
    "A1,,Alpha Central P1,3.1000,101.6000,0,A\n"
    "A2,,Alpha Central P2,3.1001,101.6001,0,A\n"
    "B,BBB,Bravo,3.2000,101.6500,0,\n"
    "C,CCC,Charlie,3.3000,101.7000,0,\n"
    "D,DDD,Delta,3.5000,101.8000,0,\n"
    "E,EEE,Echo,3.8000,101.9000,0,\n"
    "X,XXX,Bus stop,3.0,101.0,0,\n"
)

STOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    # Platinum: A -> C -> E, 08:00 to 10:00
    "t1,08:00:00,08:00:00,A1,1\n"
    "t1,08:40:00,08:42:00,C,2\n"
    "t1,10:00:00,10:00:00,E,3\n"
    # Gold: A -> B -> C -> D -> E, 09:00 to 11:30, sequence given out of order on purpose
    "t2,10:10:00,10:12:00,D,4\n"
    "t2,09:00:00,09:00:00,A2,1\n"
    "t2,09:20:00,09:21:00,B,2\n"
    "t2,09:50:00,09:52:00,C,3\n"
    "t2,11:30:00,11:30:00,E,5\n"
    # Komuter k1 and k2 share the exact relative pattern, k3 too (weekend copy of k1)
    "k1,07:00:00,07:00:00,A1,1\n"
    "k1,07:15:00,07:16:00,B,2\n"
    "k1,07:35:00,07:35:00,C,3\n"
    "k2,07:30:00,07:30:00,A1,1\n"
    "k2,07:45:00,07:46:00,B,2\n"
    "k2,08:05:00,08:05:00,C,3\n"
    "k3,07:00:00,07:00:00,A1,1\n"
    "k3,07:15:00,07:16:00,B,2\n"
    "k3,07:35:00,07:35:00,C,3\n"
    "b1,07:00:00,07:00:00,X,1\n"
    "b1,07:10:00,07:10:00,A1,2\n"
)

SHAPES = (
    "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
    "s1,3.1000,101.6000,1\n"
    "s1,3.1500,101.6250,2\n"  # on the straight line A->B: should be simplified away
    "s1,3.2000,101.6500,3\n"
    "s1,3.3000,101.7000,4\n"
    "s1,3.5000,101.8000,5\n"
    "s1,3.8000,101.9000,6\n"
)


def write_feed(folder: str, overnight: bool = False) -> None:
    os.makedirs(folder, exist_ok=True)
    files = {
        "agency.txt": AGENCY,
        "routes.txt": ROUTES,
        "trips.txt": TRIPS,
        "stops.txt": "﻿" + STOPS,  # BOM on purpose
        "stop_times.txt": STOP_TIMES,
        "shapes.txt": SHAPES,
    }
    if overnight:
        files["trips.txt"] += "ETS,WK,n1,E Town,EG9999,\n"
        files["stop_times.txt"] += "n1,23:30:00,23:30:00,A1,1\nn1,24:45:00,24:45:00,E,2\n"
    for name, content in files.items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write(content)


class BuildBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.feed_dir = os.path.join(self.tmp, "feed")
        write_feed(self.feed_dir, overnight=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def build(self, path=None):
        b = bb.BundleBuilder()
        b.add_feed("tst", bb.Feed(path or self.feed_dir))
        return b, b.build()

    def test_skips_non_rail_routes_and_their_stops(self):
        _, bundle = self.build()
        self.assertEqual({r["id"] for r in bundle["routes"]}, {"tst:ETS", "tst:KC"})
        self.assertNotIn("tst:X", {s["id"] for s in bundle["stations"]})

    def test_platforms_collapse_to_parent_station(self):
        _, bundle = self.build()
        ids = [s["id"] for s in bundle["stations"]]
        self.assertIn("tst:A", ids)
        self.assertNotIn("tst:A1", ids)
        self.assertNotIn("tst:A2", ids)
        alpha = bundle["stations"][ids.index("tst:A")]
        self.assertEqual(alpha["name"], "Alpha Central")
        self.assertEqual(alpha["code"], "AAA")

    def test_classification(self):
        _, bundle = self.build()
        services = {r["id"]: r["service"] for r in bundle["routes"]}
        self.assertEqual(services["tst:ETS"], "ETS")
        self.assertEqual(services["tst:KC"], "KOMUTER")
        by_label = {}
        for p in bundle["patterns"]:
            for label in p["labels"]:
                by_label[label] = p["service"]
        self.assertEqual(by_label["EP9001"], "ETS_PLATINUM")
        self.assertEqual(by_label["EG9101"], "ETS_GOLD")

    def test_identical_patterns_merge_departures(self):
        _, bundle = self.build()
        komuter = [p for p in bundle["patterns"] if p["service"] == "KOMUTER"]
        self.assertEqual(len(komuter), 1)
        self.assertEqual(komuter[0]["departures"], [7 * 3600, 7 * 3600 + 1800])
        self.assertEqual(komuter[0]["labels"], [])  # no trip_short_name on Komuter trips

    def test_relative_times_and_stop_order(self):
        _, bundle = self.build()
        gold = next(p for p in bundle["patterns"] if "EG9101" in p["labels"])
        names = [bundle["stations"][i]["name"] for i, _, _ in gold["stops"]]
        self.assertEqual(names, ["Alpha Central", "Bravo", "Charlie", "Delta", "Echo"])
        self.assertEqual(gold["stops"][0][1:], [0, 0])
        self.assertEqual(gold["stops"][1][1:], [20 * 60, 21 * 60])
        self.assertEqual(gold["stops"][-1][1], 150 * 60)

    def test_overnight_trip_keeps_positive_duration(self):
        _, bundle = self.build()
        n1 = next(p for p in bundle["patterns"] if "EG9999" in p["labels"])
        self.assertEqual(n1["stops"][-1][1], 75 * 60)
        self.assertEqual(n1["departures"], [23 * 3600 + 30 * 60])

    def test_shape_is_simplified_and_namespaced(self):
        _, bundle = self.build()
        self.assertEqual(len(bundle["shapes"]), 1)
        shape = bundle["shapes"][0]
        self.assertEqual(shape["id"], "tst:s1")
        # A, B, C and D sit on one straight line, so only A, D and E survive.
        self.assertEqual(len(shape["points"]) // 2, 3)
        pattern = next(p for p in bundle["patterns"] if "EP9001" in p["labels"])
        self.assertEqual(pattern["shape"], "tst:s1")
        komuter = next(p for p in bundle["patterns"] if p["service"] == "KOMUTER")
        self.assertNotIn("shape", komuter)

    def test_reads_zip_with_nested_folder(self):
        zpath = os.path.join(self.tmp, "feed.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            for name in os.listdir(self.feed_dir):
                z.write(os.path.join(self.feed_dir, name), arcname=f"gtfs/{name}")
        _, from_zip = self.build(zpath)
        _, from_dir = self.build()
        self.assertEqual(len(from_zip["patterns"]), len(from_dir["patterns"]))
        self.assertEqual(from_zip["stations"], from_dir["stations"])

    def test_cli_writes_bundle_and_report(self):
        out = os.path.join(self.tmp, "out", "rail_bundle.json")
        report = os.path.join(self.tmp, "out", "report.txt")
        code = bb.main(["--feed", f"tst={self.feed_dir}", "--feed", "missing=/nope", "--out", out, "--report", report])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["format"], 1)
        self.assertTrue(os.path.getsize(report) > 0)

    def test_classifies_real_malaysian_route_names(self):
        cases = {
            ("KTM Tanjung Malim - Pelabuhan Klang", "Port Klang Line"): "KOMUTER",
            ("KTM Batu Caves - Pulau Sebang/Tampin", "Seremban Line"): "KOMUTER",
            ("Kelana Jaya Line", "KJL"): "LRT",
            ("Sri Petaling Line", "SPL"): "LRT",
            ("Shah Alam Line", "SAL"): "LRT",
            ("Kajang Line", "KGL"): "MRT",
            ("Putrajaya Line", "PYL"): "MRT",
            ("KL Monorail Line", "MRL"): "MONORAIL",
            ("Sunway Line", "BRT"): "BRT",
            ("Intercity Shuttle Tumpat - Gemas", "SH"): "INTERCITY",
            ("Electric Train Service Padang Besar - JB Sentral", "ETS"): "ETS",
            ("Intercity Shuttle Tebrau JB Sentral - Woodlands", "ST"): "SHUTTLE_TEBRAU",
        }
        for (long, short), expected in cases.items():
            got = bb.classify_route({"route_id": "x", "route_short_name": short, "route_long_name": long})
            self.assertEqual(got, expected, long)

    def test_tidy_name(self):
        self.assertEqual(bb.tidy_name("HAT YAI"), "Hat Yai")
        self.assertEqual(bb.tidy_name("PULAU SEBANG/TAMPIN"), "Pulau Sebang/Tampin")
        self.assertEqual(bb.tidy_name("KL SENTRAL"), "KL Sentral")
        self.assertEqual(bb.tidy_name("USJ7"), "USJ7")
        self.assertEqual(bb.tidy_name("WOODLANDS CIQ"), "Woodlands CIQ")
        self.assertEqual(bb.tidy_name("PEL Klang SEL"), "PEL Klang SEL")  # mixed case: leave alone

    def test_frequencies_expand_into_departures(self):
        with open(os.path.join(self.feed_dir, "frequencies.txt"), "w") as fh:
            fh.write("trip_id,start_time,end_time,headway_secs\nk1,7:00:00,8:00:00,1200\n")
        _, bundle = self.build()
        komuter = next(p for p in bundle["patterns"] if p["service"] == "KOMUTER")
        # 07:00, 07:20, 07:40 from frequencies plus k2's own 07:30
        self.assertEqual(komuter["departures"], [25200, 26400, 27000, 27600])

    def test_numeric_trip_id_is_used_as_train_number(self):
        self.assertEqual(bb.train_label({"trip_id": "9201"}), "9201")
        self.assertEqual(bb.train_label({"trip_id": "AGL_MonFri_0"}), "")
        self.assertEqual(bb.train_label({"trip_id": "1", "trip_short_name": "EP9001"}), "EP9001")

    def test_parse_gtfs_time(self):
        self.assertEqual(bb.parse_gtfs_time("25:10:05"), 25 * 3600 + 605)
        self.assertIsNone(bb.parse_gtfs_time(""))
        self.assertIsNone(bb.parse_gtfs_time("7:61:00"))


if __name__ == "__main__":
    unittest.main()
