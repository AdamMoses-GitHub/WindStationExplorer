from __future__ import annotations

import unittest

from weather_wind_app.services.spatial import bbox_from_centroid, distance_to_km


class SpatialTests(unittest.TestCase):
    def test_distance_to_km(self) -> None:
        self.assertAlmostEqual(distance_to_km(10.0, "km"), 10.0)
        self.assertAlmostEqual(distance_to_km(10.0, "mi"), 16.09344)

    def test_bbox_from_centroid(self) -> None:
        min_lat, min_lon, max_lat, max_lon = bbox_from_centroid(40.0, -100.0, 200.0, 100.0, "km")
        self.assertLess(min_lat, 40.0)
        self.assertGreater(max_lat, 40.0)
        self.assertLess(min_lon, -100.0)
        self.assertGreater(max_lon, -100.0)


if __name__ == "__main__":
    unittest.main()
