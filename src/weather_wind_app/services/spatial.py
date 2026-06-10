"""Spatial calculations used by region queries."""

from __future__ import annotations

import math
from typing import Tuple


def distance_to_km(distance: float, unit: str) -> float:
    if unit == "km":
        return distance
    if unit == "mi":
        return distance * 1.609344
    raise ValueError(f"Unsupported unit: {unit}")


def bbox_from_centroid(
    latitude: float,
    longitude: float,
    width: float,
    height: float,
    unit: str,
) -> Tuple[float, float, float, float]:
    """Return bbox as min_lat, min_lon, max_lat, max_lon."""
    width_km = distance_to_km(width, unit)
    height_km = distance_to_km(height, unit)

    lat_delta = (height_km / 2.0) / 111.32
    cos_lat = max(math.cos(math.radians(latitude)), 0.1)
    lon_delta = (width_km / 2.0) / (111.32 * cos_lat)

    min_lat = max(latitude - lat_delta, -90.0)
    max_lat = min(latitude + lat_delta, 90.0)
    min_lon = max(longitude - lon_delta, -180.0)
    max_lon = min(longitude + lon_delta, 180.0)
    return min_lat, min_lon, max_lat, max_lon


def point_in_bbox(
    latitude: float,
    longitude: float,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
) -> bool:
    return min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon
