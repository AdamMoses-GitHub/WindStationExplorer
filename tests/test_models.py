from __future__ import annotations

import unittest
from datetime import datetime, timezone

from weather_wind_app.models import WindStationObservation


class ModelTests(unittest.TestCase):
    def test_roundtrip_dict(self) -> None:
        source = WindStationObservation(
            station_id="KABC",
            station_name="Example",
            latitude=40.0,
            longitude=-100.0,
            timestamp=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
            wind_speed_mps=5.5,
            wind_gust_mps=7.2,
            wind_direction_deg=90.0,
            source="NWS",
        )
        encoded = source.to_dict()
        restored = WindStationObservation.from_dict(encoded)

        self.assertEqual(restored.station_id, source.station_id)
        self.assertEqual(restored.station_name, source.station_name)
        self.assertAlmostEqual(restored.wind_speed_mps or 0.0, 5.5)
        self.assertEqual(restored.direction_arrow(), "E")


if __name__ == "__main__":
    unittest.main()
