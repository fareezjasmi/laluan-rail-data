# FocusTrack rail data

Open rail timetables and real track, packed for the FocusTrack focus app: every focus session is a real
train ride, on the real timetable, along the real line.

A GitHub Actions job (`.github/workflows/build-packs.yml`) rebuilds every region weekly and publishes
the packs plus `index.json` (the list the app's globe shows) to the
[`packs` release](../../releases/tag/packs). Build reports land on the `reports` branch.

| Region | Timetables | Licence |
|---|---|---|
| Malaysia | KTMB and Prasarana via data.gov.my | CC BY 4.0 |
| Germany | DELFI e.V. via gtfs.de (long-distance and regional) | CC BY 4.0 |
| France | SNCF Voyageurs (TGV, Intercités, TER) | ODbL |
| Switzerland | opentransportdata.swiss | Swiss open data terms |
| Netherlands | OVapi (NDOV open data) | open data |
| Belgium | SNCB/NMBS via gtfs.be | see gtfs.be |
| Spain | Renfe Operadora (AVE, Larga/Media Distancia, Cercanías) | Renfe open data |

Track geometry: © OpenStreetMap contributors, ODbL (Geofabrik extracts).

The app's website, privacy policy and support pages live in `docs/` (GitHub Pages).

## How a pack is made

`pipeline/build_region.py <id>`: downloads the region's GTFS feeds and OpenStreetMap extract, keeps one
real, busy midweek day of trains, names each train type (ICE, TGV, RE, S-Bahn...), routes every
station-to-station leg along the real track, and writes a compressed pack. Add a country in
`pipeline/regions.py`. Tests: `python -m unittest discover -s pipeline`.
