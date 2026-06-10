"""Shared data models for station wind data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass(slots=True)
class StationRef:
    """A weather station known to the app."""

    station_id: str
    name: str
    latitude: float
    longitude: float
    latest_observation_url: str


@dataclass(slots=True)
class WindStationObservation:
    """Normalized wind observation values from an API source."""

    station_id: str
    station_name: str
    latitude: float
    longitude: float
    timestamp: Optional[datetime]
    wind_speed_mps: Optional[float]
    wind_gust_mps: Optional[float]
    wind_direction_deg: Optional[float]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "station_name": self.station_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "wind_speed_mps": self.wind_speed_mps,
            "wind_gust_mps": self.wind_gust_mps,
            "wind_direction_deg": self.wind_direction_deg,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindStationObservation":
        raw_ts = data.get("timestamp")
        ts: Optional[datetime] = None
        if isinstance(raw_ts, str):
            try:
                ts = datetime.fromisoformat(raw_ts)
            except ValueError:
                ts = None
        return cls(
            station_id=str(data.get("station_id", "")),
            station_name=str(data.get("station_name", "")),
            latitude=float(data.get("latitude", 0.0)),
            longitude=float(data.get("longitude", 0.0)),
            timestamp=ts,
            wind_speed_mps=_float_or_none(data.get("wind_speed_mps")),
            wind_gust_mps=_float_or_none(data.get("wind_gust_mps")),
            wind_direction_deg=_float_or_none(data.get("wind_direction_deg")),
            source=str(data.get("source", "unknown")),
        )

    def speed_for_display(self, unit: str) -> Optional[float]:
        if self.wind_speed_mps is None:
            return None
        if unit == "m/s":
            return self.wind_speed_mps
        if unit == "mph":
            return self.wind_speed_mps * 2.2369362921
        if unit == "kt":
            return self.wind_speed_mps * 1.9438444924
        return self.wind_speed_mps

    def gust_for_display(self, unit: str) -> Optional[float]:
        if self.wind_gust_mps is None:
            return None
        if unit == "m/s":
            return self.wind_gust_mps
        if unit == "mph":
            return self.wind_gust_mps * 2.2369362921
        if unit == "kt":
            return self.wind_gust_mps * 1.9438444924
        return self.wind_gust_mps

    def direction_arrow(self) -> str:
        if self.wind_direction_deg is None:
            return "?"
        deg = self.wind_direction_deg % 360
        arrows = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        idx = int((deg + 22.5) // 45) % len(arrows)
        return arrows[idx]


def _float_or_none(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
