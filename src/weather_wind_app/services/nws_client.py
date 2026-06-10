"""NOAA NWS API client for station and wind observation retrieval."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests

from weather_wind_app.config import (
    DEFAULT_NEARBY_MAX_PAGES,
    MAX_STATIONS_PER_QUERY,
    NWS_BASE_URL,
    NWS_USER_AGENT,
    OBSERVATION_WORKERS,
    REQUEST_TIMEOUT_SECONDS,
)
from weather_wind_app.models import StationRef, WindStationObservation
from weather_wind_app.services.spatial import point_in_bbox


CHUNK_SIZE = 120


class NWSClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": NWS_USER_AGENT,
                "Accept": "application/geo+json, application/ld+json, application/json",
            }
        )

    def get_stations_in_bbox(
        self,
        bbox: Tuple[float, float, float, float],
        max_stations: int = MAX_STATIONS_PER_QUERY,
        logger: Optional[Callable[[str], None]] = None,
        centroid: Optional[Tuple[float, float]] = None,
        prefer_nearby: bool = False,
        allow_nationwide_fallback: bool = False,
        nearby_max_pages: int = DEFAULT_NEARBY_MAX_PAGES,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> List[StationRef]:
        min_lat, min_lon, max_lat, max_lon = bbox

        if prefer_nearby and centroid is not None:
            if logger:
                logger("Station lookup strategy: nearby-first (centroid endpoint).")
            nearby = self._get_nearby_stations(
                centroid,
                logger,
                max_pages=max(1, nearby_max_pages),
                should_cancel=should_cancel,
            )
            filtered: List[StationRef] = []
            seen_ids: set[str] = set()
            for station in nearby:
                if station.station_id in seen_ids:
                    continue
                if not point_in_bbox(
                    station.latitude,
                    station.longitude,
                    min_lat,
                    min_lon,
                    max_lat,
                    max_lon,
                ):
                    continue
                filtered.append(station)
                seen_ids.add(station.station_id)
                if len(filtered) >= max_stations:
                    break

            if filtered:
                if logger:
                    logger(
                        f"Nearby station lookup succeeded: matched={len(filtered)} "
                        "(skipped nationwide scan)."
                    )
                return filtered

            if logger:
                logger(
                    "Nearby station lookup returned no in-bbox matches; "
                    "no direct match found."
                )

            if allow_nationwide_fallback:
                if logger:
                    logger("Nationwide fallback enabled; starting full station scan.")
            else:
                if logger:
                    logger("Nationwide fallback disabled; returning 0 stations.")
                return []

        stations: List[StationRef] = []
        page_count = 0

        next_url = f"{NWS_BASE_URL}/stations?limit=500"
        while next_url and len(stations) < max_stations:
            if should_cancel and should_cancel():
                if logger:
                    logger("Station scan cancelled.")
                break
            page_count += 1
            payload = self._get_json(next_url)
            features = payload.get("features", [])
            for feature in features:
                if should_cancel and should_cancel():
                    break
                if len(stations) >= max_stations:
                    break
                geometry = feature.get("geometry", {})
                coords = geometry.get("coordinates") or []
                if len(coords) < 2:
                    continue
                lon = coords[0]
                lat = coords[1]
                if not point_in_bbox(lat, lon, min_lat, min_lon, max_lat, max_lon):
                    continue

                props = feature.get("properties", {})
                station_id = props.get("stationIdentifier") or props.get("@id", "")
                if not station_id:
                    continue

                latest_url = props.get("@id", "")
                if latest_url:
                    latest_url = latest_url.rstrip("/") + "/observations/latest"

                stations.append(
                    StationRef(
                        station_id=station_id,
                        name=props.get("name", station_id),
                        latitude=lat,
                        longitude=lon,
                        latest_observation_url=latest_url,
                    )
                )

            if logger and (page_count == 1 or page_count % 5 == 0):
                logger(
                    f"Station scan progress: pages={page_count}, matched={len(stations)}, "
                    f"max={max_stations}"
                )
            next_url = payload.get("pagination", {}).get("next")

        if logger:
            logger(
                f"Station scan finished: pages={page_count}, matched={len(stations)}"
            )

        return stations

    def _get_nearby_stations(
        self,
        centroid: Tuple[float, float],
        logger: Optional[Callable[[str], None]] = None,
        max_pages: int = 4,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> List[StationRef]:
        lat, lon = centroid
        stations: List[StationRef] = []
        page_count = 0

        next_url = f"{NWS_BASE_URL}/points/{lat},{lon}/stations"
        try:
            while next_url and page_count < max_pages:
                if should_cancel and should_cancel():
                    if logger:
                        logger("Nearby station lookup cancelled.")
                    break
                page_count += 1
                payload = self._get_json(next_url)
                features = payload.get("features", [])
                if logger:
                    logger(
                        f"Nearby station progress: pages={page_count}/{max_pages}, "
                        f"returned={len(stations)}"
                    )
                for feature in features:
                    if should_cancel and should_cancel():
                        break
                    geometry = feature.get("geometry", {})
                    coords = geometry.get("coordinates") or []
                    if len(coords) < 2:
                        continue

                    props = feature.get("properties", {})
                    station_id = props.get("stationIdentifier") or props.get("@id", "")
                    if not station_id:
                        continue

                    latest_url = props.get("@id", "")
                    if latest_url:
                        latest_url = latest_url.rstrip("/") + "/observations/latest"

                    stations.append(
                        StationRef(
                            station_id=station_id,
                            name=props.get("name", station_id),
                            latitude=coords[1],
                            longitude=coords[0],
                            latest_observation_url=latest_url,
                        )
                    )

                next_url = payload.get("pagination", {}).get("next")

            if logger:
                logger(
                    f"Nearby station endpoint complete: pages={page_count}, returned={len(stations)}"
                )
            return stations
        except Exception as exc:
            if logger:
                logger(f"Nearby station endpoint failed: {exc}")
            return []

    def fetch_latest_observations(
        self,
        stations: Iterable[StationRef],
        logger: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> List[WindStationObservation]:
        station_list = list(stations)
        observations: List[WindStationObservation] = []
        total_chunks = (len(station_list) + CHUNK_SIZE - 1) // CHUNK_SIZE
        chunk_idx = 0
        failed_count = 0

        for chunk in _chunks(station_list, CHUNK_SIZE):
            if should_cancel and should_cancel():
                if logger:
                    logger("Latest observation fetch cancelled.")
                break
            chunk_idx += 1
            with ThreadPoolExecutor(max_workers=OBSERVATION_WORKERS) as executor:
                future_to_station = {
                    executor.submit(self._fetch_station_latest, station): station
                    for station in chunk
                }
                for future in as_completed(future_to_station):
                    if should_cancel and should_cancel():
                        break
                    station = future_to_station[future]
                    result, error = future.result()
                    if result is not None:
                        observations.append(result)
                    elif error and logger:
                        failed_count += 1
                        logger(f"Station {station.station_id} failed: {error}")
            if logger:
                logger(
                    f"Latest observation chunk {chunk_idx}/{total_chunks}: "
                    f"stations={len(chunk)}, observations={len(observations)}"
                )

        if failed_count and logger:
            logger(f"Latest observation fetch complete: {failed_count} station(s) failed (network/parse errors).")
        observations.sort(key=lambda o: o.station_id)
        return observations

    def fetch_historical_observations(
        self,
        stations: Iterable[StationRef],
        start_iso: str,
        end_iso: str,
        logger: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, List[WindStationObservation]]:
        station_list = list(stations)
        series: Dict[str, List[WindStationObservation]] = {}
        total_chunks = (len(station_list) + CHUNK_SIZE - 1) // CHUNK_SIZE
        chunk_idx = 0

        failed_hist_count = 0

        for chunk in _chunks(station_list, CHUNK_SIZE):
            if should_cancel and should_cancel():
                if logger:
                    logger("Historical observation fetch cancelled.")
                break
            chunk_idx += 1
            with ThreadPoolExecutor(max_workers=OBSERVATION_WORKERS) as executor:
                future_to_station = {
                    executor.submit(
                        self._fetch_station_range,
                        station,
                        start_iso,
                        end_iso,
                    ): station
                    for station in chunk
                }
                for future in as_completed(future_to_station):
                    if should_cancel and should_cancel():
                        break
                    station = future_to_station[future]
                    entries, error = future.result()
                    if error and logger:
                        failed_hist_count += 1
                        logger(f"Station {station.station_id} historical failed: {error}")
                    if entries:
                        if len(entries) >= 500 and logger:
                            logger(
                                f"Station {station.station_id}: received 500 observations (API limit); "
                                "results may be truncated — narrow the time range for complete data."
                            )
                        entries.sort(key=lambda e: e.timestamp or datetime.min)
                        series[station.station_id] = entries
            if logger:
                logger(
                    f"Historical chunk {chunk_idx}/{total_chunks}: "
                    f"stations={len(chunk)}, stations_with_data={len(series)}"
                )

        if failed_hist_count and logger:
            logger(f"Historical fetch complete: {failed_hist_count} station(s) failed (network/parse errors).")
        return series

    @staticmethod
    def summarize_historical_latest(
        historical_series: Dict[str, List[WindStationObservation]],
    ) -> List[WindStationObservation]:
        latest: List[WindStationObservation] = []
        for station_id in sorted(historical_series.keys()):
            entries = historical_series[station_id]
            if not entries:
                continue
            latest.append(entries[-1])
        return latest

    def _fetch_station_latest(self, station: StationRef) -> Tuple[Optional[WindStationObservation], Optional[str]]:
        if not station.latest_observation_url:
            return None, None
        try:
            payload = self._get_json(station.latest_observation_url)
            props = payload.get("properties", {})
            timestamp = _parse_timestamp(props.get("timestamp"))
            wind_speed_mps = _get_quantity_value(props.get("windSpeed"))
            wind_gust_mps = _get_quantity_value(props.get("windGust"))
            wind_direction = _get_quantity_value(props.get("windDirection"))
            return WindStationObservation(
                station_id=station.station_id,
                station_name=station.name,
                latitude=station.latitude,
                longitude=station.longitude,
                timestamp=timestamp,
                wind_speed_mps=wind_speed_mps,
                wind_gust_mps=wind_gust_mps,
                wind_direction_deg=wind_direction,
                source="NWS",
            ), None
        except Exception as exc:
            return None, str(exc)

    def _fetch_station_range(
        self,
        station: StationRef,
        start_iso: str,
        end_iso: str,
    ) -> Tuple[List[WindStationObservation], Optional[str]]:
        try:
            url = f"{NWS_BASE_URL}/stations/{station.station_id}/observations"
            resp = self.session.get(
                url,
                params={"start": start_iso, "end": end_iso, "limit": 500},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            payload = resp.json()
            features = payload.get("features", [])
            observations: List[WindStationObservation] = []
            for feature in features:
                geometry = feature.get("geometry", {})
                coords = geometry.get("coordinates") or []
                if len(coords) < 2:
                    continue
                props = feature.get("properties", {})
                timestamp = _parse_timestamp(props.get("timestamp"))
                observations.append(
                    WindStationObservation(
                        station_id=station.station_id,
                        station_name=station.name,
                        latitude=coords[1],
                        longitude=coords[0],
                        timestamp=timestamp,
                        wind_speed_mps=_get_quantity_value(props.get("windSpeed")),
                        wind_gust_mps=_get_quantity_value(props.get("windGust")),
                        wind_direction_deg=_get_quantity_value(props.get("windDirection")),
                        source="NWS-HIST",
                    )
                )
            return observations, None
        except Exception as exc:
            return [], str(exc)

    def _get_json(self, url: str) -> dict:
        resp = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()


def _get_quantity_value(quantity: object) -> Optional[float]:
    if not isinstance(quantity, dict):
        return None
    value = quantity.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _chunks(values: List[StationRef], size: int) -> Iterable[List[StationRef]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]
