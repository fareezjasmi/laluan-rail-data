"""Encoded polylines (Google's algorithm), the compact form track geometry travels in.

Precision 5 is about 1 m. The Kotlin decoder is shared/.../Geo.kt `decodePolyline`; keep the two in step.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple


def _encode_value(value: int, out: List[str]) -> None:
    value = ~(value << 1) if value < 0 else value << 1
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))


def encode(points: Sequence[Tuple[float, float]], precision: int = 5) -> str:
    factor = 10 ** precision
    out: List[str] = []
    last_lat = last_lon = 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        _encode_value(ilat - last_lat, out)
        _encode_value(ilon - last_lon, out)
        last_lat, last_lon = ilat, ilon
    return "".join(out)


def decode(text: str, precision: int = 5) -> List[Tuple[float, float]]:
    factor = 10 ** precision
    points: List[Tuple[float, float]] = []
    index = lat = lon = 0
    while index < len(text):
        for axis in range(2):
            shift = result = 0
            while True:
                b = ord(text[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / factor, lon / factor))
    return points
