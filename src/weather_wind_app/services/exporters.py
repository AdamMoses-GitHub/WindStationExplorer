"""Export helpers for CSV, JSON, and KML outputs."""

from __future__ import annotations

import csv
import json
import struct
import zlib
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List
from xml.etree import ElementTree as ET

from weather_wind_app.models import WindStationObservation


def export_csv(
    out_path: Path,
    observations: List[WindStationObservation],
    historical_series: Dict[str, List[WindStationObservation]],
    speed_unit: str,
) -> None:
    rows: Iterable[WindStationObservation]
    if historical_series:
        flattened: list[WindStationObservation] = []
        for station_id in sorted(historical_series.keys()):
            flattened.extend(historical_series[station_id])
        rows = flattened
    else:
        rows = observations

    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "station_id",
                "station_name",
                "latitude",
                "longitude",
                "timestamp",
                f"wind_speed_{_unit_name(speed_unit)}",
                f"wind_gust_{_unit_name(speed_unit)}",
                "wind_direction_deg",
                "source",
            ],
        )
        writer.writeheader()
        for obs in rows:
            writer.writerow(
                {
                    "station_id": obs.station_id,
                    "station_name": obs.station_name,
                    "latitude": obs.latitude,
                    "longitude": obs.longitude,
                    "timestamp": obs.timestamp.isoformat() if obs.timestamp else "",
                    f"wind_speed_{_unit_name(speed_unit)}": _fmt(obs.speed_for_display(speed_unit)),
                    f"wind_gust_{_unit_name(speed_unit)}": _fmt(obs.gust_for_display(speed_unit)),
                    "wind_direction_deg": _fmt(obs.wind_direction_deg),
                    "source": obs.source,
                }
            )


def export_json(
    out_path: Path,
    observations: List[WindStationObservation],
    historical_series: Dict[str, List[WindStationObservation]],
    metadata: dict,
) -> None:
    payload = {
        "metadata": metadata,
        "latest_observations": [obs.to_dict() for obs in observations],
        "historical_series": {
            station_id: [entry.to_dict() for entry in series]
            for station_id, series in historical_series.items()
        },
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def export_kml(
    out_path: Path,
    observations: List[WindStationObservation],
    speed_unit: str,
    metadata: dict,
    wind_vector_mode: str = "upwind",
) -> None:
    icon_map = _icon_png_map()
    suffix = out_path.suffix.lower()

    if suffix == ".kmz":
        kml_text = _build_kml_text(
            observations,
            speed_unit,
            metadata,
            wind_vector_mode,
            icon_href_for_band=lambda band: f"icons/{band}.png",
        )
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doc.kml", kml_text)
            for band, png_bytes in icon_map.items():
                zf.writestr(f"icons/{band}.png", png_bytes)
        return

    assets_dir = out_path.parent / f"{out_path.stem}_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for band, png_bytes in icon_map.items():
        (assets_dir / f"{band}.png").write_bytes(png_bytes)

    kml_text = _build_kml_text(
        observations,
        speed_unit,
        metadata,
        wind_vector_mode,
        icon_href_for_band=lambda band: f"{assets_dir.name}/{band}.png",
    )
    out_path.write_text(kml_text, encoding="utf-8")


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


def _unit_name(speed_unit: str) -> str:
    if speed_unit == "m/s":
        return "mps"
    return speed_unit


def _build_kml_text(
    observations: List[WindStationObservation],
    speed_unit: str,
    metadata: dict,
    wind_vector_mode: str,
    icon_href_for_band,
) -> str:
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml, "Document")

    name = ET.SubElement(document, "name")
    name.text = "US Wind Sensor Export"

    desc = ET.SubElement(document, "description")
    desc.text = json.dumps(metadata)

    for obs in observations:
        placemark = ET.SubElement(document, "Placemark")
        pm_name = ET.SubElement(placemark, "name")
        pm_name.text = f"{obs.station_id} - {obs.station_name}"

        pm_desc = ET.SubElement(placemark, "description")
        speed_val = _fmt(obs.speed_for_display(speed_unit))
        gust_val = _fmt(obs.gust_for_display(speed_unit))
        dir_val = _fmt(obs.wind_direction_deg)
        ts_val = obs.timestamp.isoformat() if obs.timestamp else "n/a"
        pm_desc.text = (
            f"Speed: {speed_val} {speed_unit}; "
            f"Gust: {gust_val} {speed_unit}; "
            f"Direction: {dir_val} deg; "
            f"Updated: {ts_val}"
        )

        style = ET.SubElement(placemark, "Style")
        icon_style = ET.SubElement(style, "IconStyle")
        scale = ET.SubElement(icon_style, "scale")
        scale.text = f"{_icon_scale(obs.wind_speed_mps):.2f}"
        heading = ET.SubElement(icon_style, "heading")
        heading.text = f"{_icon_heading(obs.wind_direction_deg, wind_vector_mode):.2f}"
        icon = ET.SubElement(icon_style, "Icon")
        href = ET.SubElement(icon, "href")
        href.text = icon_href_for_band(_speed_band(obs.wind_speed_mps))

        label_style = ET.SubElement(style, "LabelStyle")
        label_scale = ET.SubElement(label_style, "scale")
        label_scale.text = "0"

        point = ET.SubElement(placemark, "Point")
        coords = ET.SubElement(point, "coordinates")
        coords.text = f"{obs.longitude},{obs.latitude},0"

    return ET.tostring(kml, encoding="unicode", xml_declaration=False)


def _speed_band(speed_mps: float | None) -> str:
    if speed_mps is None:
        return "none"
    if speed_mps < 3:
        return "calm"
    if speed_mps < 8:
        return "breeze"
    if speed_mps < 14:
        return "windy"
    return "strong"


def _icon_heading(direction_deg: float | None, wind_vector_mode: str) -> float:
    if direction_deg is None:
        return 0.0
    heading = float(direction_deg)
    if wind_vector_mode == "downwind":
        heading += 180.0
    return heading % 360.0


def _icon_scale(speed_mps: float | None) -> float:
    if speed_mps is None:
        return 0.75
    clamped = max(0.0, min(float(speed_mps), 30.0))
    return 0.65 + (clamped / 30.0) * 0.95


def _icon_png_map() -> dict[str, bytes]:
    return {
        "none": _arrow_png((108, 117, 125)),
        "calm": _arrow_png((42, 157, 143)),
        "breeze": _arrow_png((138, 177, 125)),
        "windy": _arrow_png((244, 162, 97)),
        "strong": _arrow_png((231, 111, 81)),
    }


def _arrow_png(color_rgb: tuple[int, int, int], size: int = 64) -> bytes:
    width = size
    height = size
    cx = width // 2

    tip_y = int(height * 0.10)
    wing_y = int(height * 0.80)
    notch_y = int(height * 0.66)
    wing_offset = int(width * 0.36)

    polygon = [
        (cx, tip_y),
        (cx + wing_offset, wing_y),
        (cx, notch_y),
        (cx - wing_offset, wing_y),
    ]

    stroke = max(1, int(width * 0.04))
    fill = (color_rgb[0], color_rgb[1], color_rgb[2], 245)
    outline = (17, 24, 39, 255)

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # No PNG filter
        for x in range(width):
            inside = _point_in_polygon(x + 0.5, y + 0.5, polygon)
            border = _near_polygon_edge(x + 0.5, y + 0.5, polygon, stroke)
            if border:
                rgba = outline
            elif inside:
                rgba = fill
            else:
                rgba = (0, 0, 0, 0)
            rows.extend(rgba)

    return _encode_png_rgba(width, height, bytes(rows))


def _point_in_polygon(x: float, y: float, vertices: list[tuple[int, int]]) -> bool:
    inside = False
    j = len(vertices) - 1
    for i in range(len(vertices)):
        xi, yi = vertices[i]
        xj, yj = vertices[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _near_polygon_edge(
    x: float,
    y: float,
    vertices: list[tuple[int, int]],
    stroke_px: int,
) -> bool:
    threshold = max(1.0, stroke_px / 2.0)
    for i in range(len(vertices)):
        x1, y1 = vertices[i]
        x2, y2 = vertices[(i + 1) % len(vertices)]
        if _point_segment_distance(x, y, x1, y1, x2, y2) <= threshold:
            return True
    return False


def _point_segment_distance(
    px: float,
    py: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5


def _encode_png_rgba(width: int, height: int, raw_scanlines: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw_scanlines, level=9)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
