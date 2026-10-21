#!/usr/bin/env python3
"""Joins the regions' *.entry.json files into index.json, the world list the app's globe reads.

    python3 make_index.py dist/ --base-url https://github.com/OWNER/laluan-rail-data/releases/download/packs

A region whose build failed this week keeps last week's entry if --previous points at the old index
(the old pack stays in the release), so one broken feed never empties the globe.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

INDEX_FORMAT = 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dist", help="folder with <id>.entry.json files")
    ap.add_argument("--base-url", required=True, help="where packs are downloaded from (no trailing slash)")
    ap.add_argument("--previous", default="", help="URL or path of the current index.json, to keep regions that failed")
    ap.add_argument("--out", default="", help="output path (default: <dist>/index.json)")
    args = ap.parse_args(argv)

    regions = {}
    for path in sorted(glob.glob(os.path.join(args.dist, "*.entry.json"))):
        with open(path, encoding="utf-8") as fh:
            entry = json.load(fh)
        entry["url"] = f"{args.base_url}/{entry['pack']}"
        regions[entry["id"]] = entry

    if args.previous:
        try:
            if args.previous.startswith("http"):
                with urllib.request.urlopen(args.previous, timeout=60) as resp:
                    old = json.load(resp)
            else:
                with open(args.previous, encoding="utf-8") as fh:
                    old = json.load(fh)
            for entry in old.get("regions", []):
                if entry["id"] not in regions:
                    print(f"keeping last build of {entry['id']} ({entry.get('version')})", file=sys.stderr)
                    regions[entry["id"]] = entry
        except (OSError, ValueError, KeyError) as e:
            print(f"warning: could not read previous index ({e})", file=sys.stderr)

    if not regions:
        print("error: no regions", file=sys.stderr)
        return 1
    index = {
        "format": INDEX_FORMAT,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "regions": sorted(regions.values(), key=lambda e: (e["group"], e["name"])),
    }
    out = args.out or os.path.join(args.dist, "index.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"index: {len(regions)} regions -> {out} ({os.path.getsize(out) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
