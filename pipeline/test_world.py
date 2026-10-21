"""Tests for the country-scale pipeline: service days, European classification, encoded legs.

Run: python3 -m unittest discover -s data/pipeline
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from collections import Counter
from datetime import date

import build_bundle as bb
import polyline_codec
from test_build_bundle import write_feed


class PolylineTest(unittest.TestCase):
    def test_round_trip_and_known_value(self):
        # Google's documented example.
        pts = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
        self.assertEqual(polyline_codec.encode(pts), "_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(polyline_codec.decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@"), pts)

    def test_precision_is_about_a_metre(self):
        pts = [(52.520008, 13.404954), (48.856613, 2.352222)]
        back = polyline_codec.decode(polyline_codec.encode(pts))
        for (a, b), (c, d) in zip(pts, back):
            self.assertAlmostEqual(a, c, places=5)
            self.assertAlmostEqual(b, d, places=5)


CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    "WK,1,1,1,1,1,0,0,20261001,20261231\n"
    "WE,0,0,0,0,0,1,1,20261001,20261231\n"
)


class ServiceDayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.feed_dir = os.path.join(self.tmp, "feed")
        write_feed(self.feed_dir)
        with open(os.path.join(self.feed_dir, "calendar.txt"), "w") as fh:
            fh.write(CALENDAR)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_picks_a_busy_midweek_day(self):
        feed = bb.Feed(self.feed_dir)
        cal = bb.Calendar(feed)
        day = bb.choose_service_day(cal, Counter({"WK": 10, "WE": 3}), date(2026, 10, 22))  # a Thursday
        self.assertIn(day.weekday(), (1, 2, 3))
        self.assertGreater(day, date(2026, 10, 22))

    def test_calendar_dates_add_and_remove(self):
        with open(os.path.join(self.feed_dir, "calendar_dates.txt"), "w") as fh:
            fh.write("service_id,date,exception_type\nWK,20261027,2\nXMAS,20261027,1\n")
        cal = bb.Calendar(bb.Feed(self.feed_dir))
        self.assertEqual(cal.active(date(2026, 10, 27)), {"XMAS"})
        self.assertEqual(cal.active(date(2026, 10, 28)), {"WK"})

    def test_feed_published_ahead_uses_its_own_start(self):
        cal = bb.Calendar(bb.Feed(self.feed_dir))
        day = bb.choose_service_day(cal, Counter({"WK": 5}), date(2026, 1, 1))
        self.assertIsNotNone(day)
        self.assertGreaterEqual(day, date(2026, 10, 1))

    def test_only_that_days_trains_are_kept(self):
        b = bb.BundleBuilder()
        b.add_feed("tst", bb.Feed(self.feed_dir), service_day=date(2026, 10, 28))
        bundle = b.build()
        self.assertEqual(b.service_days["tst"], "2026-10-28")
        self.assertEqual(bundle["serviceDays"], {"tst": "2026-10-28"})
        self.assertEqual(b.stats["tst.trips_not_on_service_day"], 1)  # the weekend Komuter k3
        komuter = next(p for p in bundle["patterns"] if p["service"] == "KOMUTER")
        self.assertEqual(komuter["departures"], [7 * 3600, 7 * 3600 + 1800])


class EuropeanClassifierTest(unittest.TestCase):
    def check(self, expected_service, expected_badge, **route):
        route.setdefault("route_type", "2")
        service, badge = bb.classify_route_eu(route)
        self.assertEqual((service, badge), (expected_service, expected_badge), route)

    def test_products(self):
        self.check("HIGH_SPEED", "ICE", route_short_name="ICE")
        self.check("HIGH_SPEED", "TGV INOUI", route_short_name="TGV INOUI")
        self.check("HIGH_SPEED", "AVE", route_short_name="AVE")
        self.check("HIGH_SPEED", "RJX 65", route_short_name="RJX 65")
        self.check("NIGHT", "NJ 40421", route_short_name="NJ 40421")
        self.check("INTERCITY", "IC 2013", route_short_name="IC 2013")
        self.check("INTERCITY", "EC 7", route_short_name="EC 7")
        self.check("REGIONAL_EXPRESS", "RE 5", route_short_name="RE 5")
        self.check("REGIONAL_EXPRESS", "IR15", route_short_name="IR15")
        self.check("REGIONAL", "RB 33", route_short_name="RB 33")
        self.check("SUBURBAN", "S3", route_short_name="S3")
        self.check("SUBURBAN", "RER B", route_short_name="RER B")

    def test_names_and_agencies_when_short_name_is_missing(self):
        service, _ = bb.classify_route_eu({"route_long_name": "TER Hauts-de-France", "route_type": "2"})
        self.assertEqual(service, "REGIONAL")
        service, _ = bb.classify_route_eu({"route_long_name": "Paris - Lyon", "route_type": "2"}, agency="OUIGO")
        self.assertEqual(service, "HIGH_SPEED")
        service, _ = bb.classify_route_eu({"route_long_name": "Intercités de nuit Paris - Briançon", "route_type": "2"})
        self.assertEqual(service, "NIGHT")

    def test_extended_route_type_and_feed_default(self):
        self.assertEqual(bb.classify_route_eu({"route_long_name": "Line 9", "route_type": "101"})[0], "HIGH_SPEED")
        self.assertEqual(bb.classify_route_eu({"route_long_name": "Line 9", "route_type": "109"})[0], "SUBURBAN")
        self.assertEqual(bb.classify_route_eu({"route_long_name": "Line 9"}, default="SUBURBAN")[0], "SUBURBAN")
        self.assertEqual(bb.classify_route_eu({"route_long_name": "Line 9"})[0], "REGIONAL")

    def test_rail_only_mode_drops_trams_and_metros(self):
        self.assertTrue(bb.is_rail_route_type("2", "rail"))
        self.assertTrue(bb.is_rail_route_type("106", "rail"))
        self.assertFalse(bb.is_rail_route_type("0", "rail"))
        self.assertFalse(bb.is_rail_route_type("401", "rail"))
        self.assertTrue(bb.is_rail_route_type("0", "all"))

    def test_badges_reach_the_bundle(self):
        tmp = tempfile.mkdtemp()
        try:
            folder = os.path.join(tmp, "feed")
            write_feed(folder)
            b = bb.BundleBuilder()
            b.add_feed("tst", bb.Feed(folder), classifier="eu")
            routes = {r["id"]: r for r in b.build()["routes"]}
            self.assertEqual(routes["tst:KC"]["badge"], "KC")
            self.assertEqual(routes["tst:KC"]["service"], "REGIONAL")
        finally:
            shutil.rmtree(tmp)


class RegionPackTest(unittest.TestCase):
    def test_pack_entry_and_index(self):
        import json
        import zlib
        import build_region
        import make_index
        from regions import REGIONS, Region, FeedSource

        tmp = tempfile.mkdtemp()
        try:
            folder = os.path.join(tmp, "feed")
            write_feed(folder)
            with open(os.path.join(folder, "calendar.txt"), "w") as fh:
                fh.write(CALENDAR)
            reg = Region(id="tt", name="Testland", country="TT", group="europe",
                         feeds=[FeedSource("tst", [])], osm_pbf="", attribution=["Test data"])
            out = os.path.join(tmp, "dist")
            entry = build_region.build(reg, [("tst", folder, "")], None, out, today=date(2026, 10, 22))
            with open(os.path.join(out, "tt.json.deflate"), "rb") as fh:
                bundle = json.loads(zlib.decompress(fh.read(), -15))
            self.assertEqual(bundle["region"], {"id": "tt", "name": "Testland", "country": "TT"})
            self.assertEqual(entry["packBytes"], os.path.getsize(os.path.join(out, "tt.json.deflate")))
            self.assertEqual(entry["stations"], 5)
            self.assertTrue(entry["lines"])
            self.assertEqual(len(entry["bbox"]), 4)

            self.assertEqual(make_index.main([out, "--base-url", "https://example.com/packs"]), 0)
            with open(os.path.join(out, "index.json")) as fh:
                index = json.load(fh)
            self.assertEqual(index["regions"][0]["url"], "https://example.com/packs/tt.json.deflate")
            # A region missing this week keeps last week's entry.
            old = os.path.join(tmp, "old.json")
            with open(old, "w") as fh:
                json.dump({"regions": [{"id": "zz", "name": "Old", "group": "asia", "version": "1"}]}, fh)
            make_index.main([out, "--base-url", "https://example.com/packs", "--previous", old])
            with open(os.path.join(out, "index.json")) as fh:
                self.assertEqual({r["id"] for r in json.load(fh)["regions"]}, {"tt", "zz"})
        finally:
            shutil.rmtree(tmp)

    def test_year_placeholders_expand_to_this_and_next_year(self):
        import build_region
        urls = build_region.expand_years(["https://x/t-{year}/permalink", "https://y/feed.zip"], date(2026, 10, 22))
        self.assertEqual(urls, ["https://x/t-2026/permalink", "https://x/t-2027/permalink", "https://y/feed.zip"])

    def test_every_region_is_well_formed(self):
        from regions import REGIONS
        ids = [r.id for r in REGIONS]
        self.assertEqual(len(ids), len(set(ids)))
        for r in REGIONS:
            self.assertTrue(r.feeds and r.attribution and r.osm_pbf.endswith(".osm.pbf"), r.id)
            self.assertIn(r.classifier, ("my", "eu"))
            self.assertEqual(len(r.country), 2)


if __name__ == "__main__":
    unittest.main()
