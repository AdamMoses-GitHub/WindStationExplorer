# Phase 0 Contracts

## Source Priority
1. Primary live source: NOAA National Weather Service API (`https://api.weather.gov`).
2. Primary recent historical source (Phase 4): NOAA NWS station observations endpoint.
3. Optional deeper historical source: NOAA CDO API with token.

## Live Endpoints
- `GET /stations?limit=500` (paged) for station discovery.
- `GET /stations/{stationId}/observations/latest` for latest station wind.

## Required Headers
- `User-Agent: USWindSensorExplorer/0.1 (github-copilot)`
- `Accept: application/geo+json, application/ld+json, application/json`

## Normalized Observation Schema
- `station_id: str`
- `station_name: str`
- `latitude: float`
- `longitude: float`
- `timestamp: datetime | null`
- `wind_speed_mps: float | null`
- `wind_gust_mps: float | null`
- `wind_direction_deg: float | null`
- `source: str`

## Spatial Query Input Contract
- Centroid latitude and longitude.
- Width and height.
- Unit selectable in `km` or `mi`.
- Converted to bbox (`min_lat`, `min_lon`, `max_lat`, `max_lon`).

## Scheduler Contract
- Default refresh: 300 seconds.
- Minimum allowed refresh: 10 seconds.

## Error Categories
- Transport: network timeout, DNS failures, connection reset.
- HTTP: non-2xx responses.
- Parse: malformed API payloads or missing expected fields.
- Data quality: null wind fields or missing timestamps.
- Rate/volume: very large area queries requiring pagination and throttling.

## Token Requirements (Phase 4)
- NOAA CDO token is configured in the GUI settings panel and persisted with Qt settings.
