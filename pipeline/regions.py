"""Every region the app offers, where its open timetables and track come from, and how to credit them.

A region becomes one downloadable pack (build_region.py) and one entry in the world index the globe
reads (make_index.py). To add a country: add a Region here, run build_region.py for it locally or let
the weekly GitHub Actions job pick it up.

Each feed lists URLs in order of preference; the first that downloads as a valid GTFS zip is used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class FeedSource:
    prefix: str
    urls: List[str] = field(default_factory=list)
    # CKAN portal + dataset name pattern ({year} filled in) for feeds that publish a new file per release.
    ckan: Optional[tuple] = None
    # Service code for routes the classifier can't place (e.g. a whole feed of suburban lines).
    default_service: str = ""


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    country: str          # ISO 3166-1 alpha-2
    group: str            # globe grouping: "asia", "europe", ...
    feeds: List[FeedSource]
    osm_pbf: str          # Geofabrik extract
    attribution: List[str]
    classifier: str = "eu"
    modes: str = "rail"
    service_day: Optional[str] = "auto"


GEOFABRIK = "https://download.geofabrik.de"
OSM_CREDIT = "Track: © OpenStreetMap contributors (ODbL)"

REGIONS: List[Region] = [
    Region(
        id="my", name="Malaysia", country="MY", group="asia",
        feeds=[
            FeedSource("ktmb", ["https://api.data.gov.my/gtfs-static/ktmb"]),
            FeedSource("rapidkl", ["https://api.data.gov.my/gtfs-static/prasarana?category=rapid-rail-kl"]),
        ],
        osm_pbf=f"{GEOFABRIK}/asia/malaysia-singapore-brunei-latest.osm.pbf",
        attribution=["Timetables: KTMB and Prasarana via data.gov.my (CC BY 4.0)", OSM_CREDIT],
        classifier="my", modes="all", service_day=None,
    ),
    Region(
        id="de", name="Germany", country="DE", group="europe",
        feeds=[
            # Long-distance (ICE, IC, EC, night trains) and regional (RE, RB, S-Bahn); they share stop ids.
            FeedSource("de", ["https://download.gtfs.de/germany/fv_free/latest.zip"]),
            FeedSource("de", ["https://download.gtfs.de/germany/rv_free/latest.zip"]),
        ],
        osm_pbf=f"{GEOFABRIK}/europe/germany-latest.osm.pbf",
        attribution=["Timetables: DELFI e.V. via gtfs.de (CC BY 4.0)", OSM_CREDIT],
    ),
    Region(
        id="fr", name="France", country="FR", group="europe",
        feeds=[FeedSource("fr", ["https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"])],
        osm_pbf=f"{GEOFABRIK}/europe/france-latest.osm.pbf",
        attribution=["Timetables: SNCF Voyageurs (TGV, Intercités, TER), ODbL", OSM_CREDIT],
    ),
    Region(
        id="ch", name="Switzerland", country="CH", group="europe",
        feeds=[FeedSource("ch", ckan=("https://data.opentransportdata.swiss", "timetable-{year}-gtfs2020"))],
        osm_pbf=f"{GEOFABRIK}/europe/switzerland-latest.osm.pbf",
        attribution=["Timetables: opentransportdata.swiss (Swiss open data terms)", OSM_CREDIT],
    ),
    Region(
        id="nl", name="Netherlands", country="NL", group="europe",
        feeds=[FeedSource("nl", ["http://gtfs.ovapi.nl/nl/gtfs-nl.zip"])],
        osm_pbf=f"{GEOFABRIK}/europe/netherlands-latest.osm.pbf",
        attribution=["Timetables: OVapi (gtfs.ovapi.nl), from NDOV open data", OSM_CREDIT],
    ),
    Region(
        id="be", name="Belgium", country="BE", group="europe",
        feeds=[FeedSource("be", ["https://data.gtfs.be/sncb/gtfs/be-sncb-gtfs.zip"])],
        osm_pbf=f"{GEOFABRIK}/europe/belgium-latest.osm.pbf",
        attribution=["Timetables: SNCB/NMBS via gtfs.be", OSM_CREDIT],
    ),
    Region(
        id="es", name="Spain", country="ES", group="europe",
        feeds=[
            FeedSource("es", ["https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"]),
            FeedSource("escer", ["https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip"],
                       default_service="SUBURBAN"),
        ],
        osm_pbf=f"{GEOFABRIK}/europe/spain-latest.osm.pbf",
        attribution=["Timetables: Renfe Operadora open data (AVE, Larga y Media Distancia, Cercanías)", OSM_CREDIT],
    ),
]


def region(region_id: str) -> Region:
    for r in REGIONS:
        if r.id == region_id:
            return r
    raise KeyError(f"unknown region {region_id!r}; known: {', '.join(r.id for r in REGIONS)}")
