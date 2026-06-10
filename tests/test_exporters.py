from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from weather_wind_app.models import WindStationObservation
from weather_wind_app.services.exporters import export_csv, export_json, export_kml


class ExporterTests(unittest.TestCase):
    def test_export_files_created(self) -> None:
        obs = [
            WindStationObservation(
                station_id="KXYZ",
                station_name="Station X",
                latitude=39.0,
                longitude=-97.0,
                timestamp=datetime(2026, 6, 9, 12, 30, tzinfo=timezone.utc),
                wind_speed_mps=4.0,
                wind_gust_mps=6.0,
                wind_direction_deg=180.0,
                source="NWS",
            )
        ]
        hist = {"KXYZ": obs}
        metadata = {"mode": "live"}

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "out.csv"
            json_path = Path(tmp) / "out.json"
            kml_path = Path(tmp) / "out.kml"

            export_csv(csv_path, obs, hist, "mph")
            export_json(json_path, obs, hist, metadata)
            export_kml(kml_path, obs, "mph", metadata)

            self.assertTrue(csv_path.exists())
            self.assertTrue(json_path.exists())
            self.assertTrue(kml_path.exists())
            self.assertIn("Station X", csv_path.read_text(encoding="utf-8"))
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["metadata"]["mode"], "live")
            self.assertIn("Placemark", kml_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
