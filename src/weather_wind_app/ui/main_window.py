"""Main desktop window for the wind sensor explorer."""

from __future__ import annotations

import json
import math
import statistics
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from PySide6.QtCore import QObject, QDateTime, QSettings, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtCharts import QBarCategoryAxis, QBarSeries, QBarSet, QChart, QChartView, QValueAxis
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from weather_wind_app.config import (
    APP_NAME,
    CACHE_DIR,
    DEFAULT_CENTROID_LAT,
    DEFAULT_CENTROID_LON,
    DEFAULT_DIMENSION_UNIT,
    DEFAULT_HEIGHT,
    DEFAULT_HISTORY_HOURS,
    DEFAULT_NEARBY_MAX_PAGES,
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_WIDTH,
    HIST_CACHE_TTL_SECONDS,
    LIVE_CACHE_TTL_SECONDS,
    MAX_LOG_LINES,
    MAX_NEARBY_MAX_PAGES,
    MAX_STATIONS_PER_QUERY,
    MAP_HTML_PATH,
    MIN_NEARBY_MAX_PAGES,
    MIN_REFRESH_SECONDS,
    SUPPORTED_DIMENSION_UNITS,
)
from weather_wind_app.models import WindStationObservation
from weather_wind_app.services.cache_store import CacheStore
from weather_wind_app.services.exporters import export_csv, export_json, export_kml
from weather_wind_app.services.nws_client import NWSClient
from weather_wind_app.services.spatial import bbox_from_centroid, distance_to_km, point_in_bbox


USA_METRO_CENTROIDS: dict[str, tuple[float, float]] = {
    "New York City": (40.7128, -74.0060),
    "Los Angeles": (34.0522, -118.2437),
    "Chicago": (41.8781, -87.6298),
    "Dallas-Fort Worth": (32.7767, -96.7970),
    "Houston": (29.7604, -95.3698),
    "Washington, DC": (38.9072, -77.0369),
    "Miami": (25.7617, -80.1918),
    "Philadelphia": (39.9526, -75.1652),
    "Atlanta": (33.7490, -84.3880),
    "Phoenix": (33.4484, -112.0740),
    "Boston": (42.3601, -71.0589),
    "Riverside-San Bernardino": (34.1083, -117.2898),
    "San Francisco Bay Area": (37.7749, -122.4194),
    "Detroit": (42.3314, -83.0458),
    "Seattle": (47.6062, -122.3321),
    "Minneapolis-St Paul": (44.9778, -93.2650),
    "San Diego": (32.7157, -117.1611),
    "Tampa-St Petersburg": (27.9506, -82.4572),
    "Denver": (39.7392, -104.9903),
    "Baltimore": (39.2904, -76.6122),
}

STATION_META_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class MapBridge(QObject):
    station_clicked = Signal(str)
    prevailing_marker_placed = Signal(float, float)
    prevailing_marker_rejected = Signal(str)

    @Slot(str)
    def markerClicked(self, station_id: str) -> None:
        self.station_clicked.emit(station_id)

    @Slot(float, float)
    def prevailingMarkerPlaced(self, latitude: float, longitude: float) -> None:
        self.prevailing_marker_placed.emit(latitude, longitude)

    @Slot(str)
    def prevailingMarkerRejected(self, message: str) -> None:
        self.prevailing_marker_rejected.emit(message)


class SortableTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other: object) -> bool:
        if not isinstance(other, QTableWidgetItem):
            return super().__lt__(other)
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if left is not None and right is not None:
            try:
                return left < right
            except TypeError:
                pass
        return self.text() < other.text()


class StatisticsDashboardDialog(QDialog):
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Computed Statistics Dashboard")
        self.resize(1200, 760)

        root_layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self.snapshot_label = QLabel("Snapshot: n/a")
        self.auto_refresh_check = QCheckBox("Live auto-refresh while open")
        self.auto_refresh_check.setChecked(True)
        self.compare_previous_check = QCheckBox("Compare With Previous Fetch")
        self.compare_previous_check.setChecked(True)
        self.compare_previous_check.toggled.connect(self.refresh_requested.emit)
        self.bins_input = QSpinBox()
        self.bins_input.setRange(3, 20)
        self.bins_input.setValue(8)
        self.bins_input.setPrefix("Bins: ")
        self.bins_input.valueChanged.connect(self.refresh_requested.emit)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Light", "Dark", "Blue Cerulean", "Brown Sand"])
        self.theme_combo.currentTextChanged.connect(self.refresh_requested.emit)
        refresh_btn = QPushButton("Refresh Now")
        refresh_btn.clicked.connect(self.refresh_requested.emit)
        export_btn = QPushButton("Export Charts PNG")
        export_btn.clicked.connect(self._export_charts_png)
        controls.addWidget(self.snapshot_label)
        controls.addStretch(1)
        controls.addWidget(self.auto_refresh_check)
        controls.addWidget(self.compare_previous_check)
        controls.addWidget(self.bins_input)
        controls.addWidget(self.theme_combo)
        controls.addWidget(refresh_btn)
        controls.addWidget(export_btn)

        kpi_grid = QGridLayout()
        self.kpi_visible = self._kpi_card("Visible Stations", "0")
        self.kpi_stale = self._kpi_card("Stale Count", "0")
        self.kpi_direction = self._kpi_card("Circular Mean Dir", "n/a")
        self.kpi_speed_triplet = self._metric_triplet_card("Wind Speed")
        self.kpi_gust_triplet = self._metric_triplet_card("Wind Gust")
        self.kpi_lapsed_min = self._kpi_card("Min Lapsed", "n/a")
        self.kpi_lapsed_mean = self._kpi_card("Mean Lapsed", "n/a")
        self.kpi_lapsed_max = self._kpi_card("Max Lapsed", "n/a")
        kpi_grid.addWidget(self.kpi_visible[0], 0, 0)
        kpi_grid.addWidget(self.kpi_stale[0], 0, 1)
        kpi_grid.addWidget(self.kpi_direction[0], 0, 2)
        kpi_grid.addWidget(self.kpi_speed_triplet[0], 1, 0, 1, 2)
        kpi_grid.addWidget(self.kpi_gust_triplet[0], 1, 2, 1, 2)
        kpi_grid.addWidget(self.kpi_lapsed_min[0], 2, 0)
        kpi_grid.addWidget(self.kpi_lapsed_mean[0], 2, 1)
        kpi_grid.addWidget(self.kpi_lapsed_max[0], 2, 2)

        chart_tabs = QTabWidget()

        dist_tab = QWidget()
        dist_layout = QGridLayout(dist_tab)
        self.speed_chart = QChartView()
        self.gust_chart = QChartView()
        dist_layout.addWidget(self.speed_chart, 0, 0)
        dist_layout.addWidget(self.gust_chart, 0, 1)

        dir_tab = QWidget()
        dir_layout = QGridLayout(dir_tab)
        self.direction_chart = QChartView()
        self.freshness_chart = QChartView()
        dir_layout.addWidget(self.direction_chart, 0, 0)
        dir_layout.addWidget(self.freshness_chart, 0, 1)

        delta_tab = QWidget()
        delta_layout = QVBoxLayout(delta_tab)
        self.delta_output = QTextEdit()
        self.delta_output.setReadOnly(True)
        delta_layout.addWidget(self.delta_output)

        chart_tabs.addTab(dist_tab, "Distributions")
        chart_tabs.addTab(dir_tab, "Direction & Freshness")
        chart_tabs.addTab(delta_tab, "Delta")

        root_layout.addLayout(controls)
        root_layout.addLayout(kpi_grid)
        root_layout.addWidget(chart_tabs)

    def auto_refresh_enabled(self) -> bool:
        return self.auto_refresh_check.isChecked()

    def histogram_bins(self) -> int:
        return self.bins_input.value()

    def compare_previous_enabled(self) -> bool:
        return self.compare_previous_check.isChecked()

    def set_statistics(
        self,
        stats: dict,
        previous_stats: dict | None,
        speed_unit: str,
        stale_minutes: int,
        delta_lines: list[str],
    ) -> None:
        self.snapshot_label.setText(f"Snapshot: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        counts = stats.get("counts", {})
        speed_stats = stats.get("speed", {})
        gust_stats = stats.get("gust", {})
        direction_stats = stats.get("direction", {})
        freshness_stats = stats.get("freshness", {})

        self.kpi_visible[1].setText(str(counts.get("total", 0)))
        self.kpi_stale[1].setText(f"{counts.get('stale_count', 0)} (>{stale_minutes}m)")
        self._set_metric_triplet(self.kpi_speed_triplet[1], speed_stats, speed_unit)
        self._set_metric_triplet(self.kpi_gust_triplet[1], gust_stats, speed_unit)
        self.kpi_direction[1].setText(f"{_fmt_optional(direction_stats.get('circular_mean_deg'), 1)} deg")
        self.kpi_lapsed_min[1].setText(_format_lapsed_seconds_value(freshness_stats.get("min_lapsed_seconds")))
        self.kpi_lapsed_mean[1].setText(_format_lapsed_seconds_value(freshness_stats.get("mean_lapsed_seconds")))
        self.kpi_lapsed_max[1].setText(_format_lapsed_seconds_value(freshness_stats.get("max_lapsed_seconds")))

        bins = self.histogram_bins()
        raw_current = stats.get("raw", {}) if isinstance(stats.get("raw", {}), dict) else {}
        raw_previous = (previous_stats or {}).get("raw", {}) if isinstance((previous_stats or {}).get("raw", {}), dict) else {}

        current_speed_hist = _histogram(
            [float(v) for v in raw_current.get("speed_values", [])],
            bins=bins,
        )
        current_gust_hist = _histogram(
            [float(v) for v in raw_current.get("gust_values", [])],
            bins=bins,
        )
        previous_speed_hist = _histogram(
            [float(v) for v in raw_previous.get("speed_values", [])],
            bins=bins,
        )
        previous_gust_hist = _histogram(
            [float(v) for v in raw_previous.get("gust_values", [])],
            bins=bins,
        )

        if self.compare_previous_enabled():
            self._set_compare_bar_chart(
                self.speed_chart,
                f"Speed Histogram ({speed_unit})",
                current_speed_hist,
                previous_speed_hist,
            )
            self._set_compare_bar_chart(
                self.gust_chart,
                f"Gust Histogram ({speed_unit})",
                current_gust_hist,
                previous_gust_hist,
            )
        else:
            self._set_bar_chart(self.speed_chart, f"Speed Histogram ({speed_unit})", current_speed_hist)
            self._set_bar_chart(self.gust_chart, f"Gust Histogram ({speed_unit})", current_gust_hist)

        sector = direction_stats.get("sector_distribution", {})
        sector_buckets = [{"label": key, "count": int(sector.get(key, 0))} for key in ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]]
        self._set_bar_chart(self.direction_chart, "Direction Sector Distribution", sector_buckets)

        freshness = stats.get("freshness", {})
        self._set_bar_chart(
            self.freshness_chart,
            "Freshness Buckets",
            freshness.get("bucket_distribution", []),
        )

        self.delta_output.setPlainText("\n".join(delta_lines))

    def _set_bar_chart(self, view: QChartView, title: str, buckets: list[dict]) -> None:
        chart = QChart()
        chart.setTitle(title)
        chart.setTheme(self._chart_theme())
        if not buckets:
            buckets = [{"label": "n/a", "count": 0}]

        categories: list[str] = []
        bar_set = QBarSet("Count")
        max_count = 0
        for bucket in buckets:
            label = str(bucket.get("label", ""))
            count = int(bucket.get("count", 0))
            categories.append(label)
            bar_set.append(count)
            max_count = max(max_count, count)

        series = QBarSeries()
        series.append(bar_set)
        chart.addSeries(series)

        axis_x = QBarCategoryAxis()
        axis_x.append(categories)
        axis_y = QValueAxis()
        axis_y.setLabelFormat("%d")
        axis_y.setRange(0, max(1, max_count))

        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)
        chart.legend().setVisible(False)

        view.setChart(chart)

    def _set_compare_bar_chart(
        self,
        view: QChartView,
        title: str,
        current_buckets: list[dict],
        previous_buckets: list[dict],
    ) -> None:
        chart = QChart()
        chart.setTitle(title)
        chart.setTheme(self._chart_theme())

        current_map = {str(item.get("label", "")): int(item.get("count", 0)) for item in current_buckets}
        previous_map = {str(item.get("label", "")): int(item.get("count", 0)) for item in previous_buckets}
        categories = list(dict.fromkeys(list(current_map.keys()) + list(previous_map.keys())))
        if not categories:
            categories = ["n/a"]

        current_set = QBarSet("Current")
        previous_set = QBarSet("Previous")
        max_count = 0
        for category in categories:
            current_count = current_map.get(category, 0)
            previous_count = previous_map.get(category, 0)
            current_set.append(current_count)
            previous_set.append(previous_count)
            max_count = max(max_count, current_count, previous_count)

        series = QBarSeries()
        series.append(current_set)
        series.append(previous_set)
        chart.addSeries(series)

        axis_x = QBarCategoryAxis()
        axis_x.append(categories)
        axis_y = QValueAxis()
        axis_y.setLabelFormat("%d")
        axis_y.setRange(0, max(1, max_count))

        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)
        chart.legend().setVisible(True)

        view.setChart(chart)

    def _chart_theme(self) -> QChart.ChartTheme:
        selected = self.theme_combo.currentText()
        if selected == "Dark":
            return QChart.ChartTheme.ChartThemeDark
        if selected == "Blue Cerulean":
            return QChart.ChartTheme.ChartThemeBlueCerulean
        if selected == "Brown Sand":
            return QChart.ChartTheme.ChartThemeBrownSand
        return QChart.ChartTheme.ChartThemeLight

    def _kpi_card(self, title: str, value: str) -> tuple[QGroupBox, QLabel]:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        value_label = QLabel(value)
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(value_label)
        return box, value_label

    def _metric_triplet_card(self, title: str) -> tuple[QGroupBox, dict[str, QLabel]]:
        box = QGroupBox(f"{title} (min / mean / max)")
        layout = QHBoxLayout(box)
        min_label = QLabel("min: n/a")
        mean_label = QLabel("mean: n/a")
        max_label = QLabel("max: n/a")
        layout.addWidget(min_label)
        layout.addWidget(mean_label)
        layout.addWidget(max_label)
        return box, {"min": min_label, "mean": mean_label, "max": max_label}

    def _set_metric_triplet(self, labels: dict[str, QLabel], stats_obj: dict, unit: str) -> None:
        labels["min"].setText(f"min: {_fmt_optional(stats_obj.get('min'), 2)} {unit}")
        labels["mean"].setText(f"mean: {_fmt_optional(stats_obj.get('mean'), 2)} {unit}")
        labels["max"].setText(f"max: {_fmt_optional(stats_obj.get('max'), 2)} {unit}")

    @Slot()
    def _export_charts_png(self) -> None:
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Export Charts PNG",
            "statistics_charts.png",
            "PNG (*.png)",
        )
        if not out:
            return
        base = Path(out)
        exported = [
            (self.speed_chart, f"{base.stem}_speed.png"),
            (self.gust_chart, f"{base.stem}_gust.png"),
            (self.direction_chart, f"{base.stem}_direction.png"),
            (self.freshness_chart, f"{base.stem}_freshness.png"),
        ]
        for chart_view, name in exported:
            target = base.parent / name
            chart_view.grab().save(str(target), "PNG")
        QMessageBox.information(self, "Export Complete", f"Charts exported to {base.parent}")


class FetchThread(QThread):
    fetch_success = Signal(object, object, object, object)
    fetch_error = Signal(str)
    fetch_log = Signal(str)
    fetch_progress = Signal(int, str)

    def __init__(
        self,
        mode: str,
        latitude: float,
        longitude: float,
        width: float,
        height: float,
        unit: str,
        allow_nationwide_fallback: bool,
        nearby_max_pages: int,
        history_start_iso: str,
        history_end_iso: str,
        cache_dir: Path,
        nws_client: NWSClient | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.latitude = latitude
        self.longitude = longitude
        self.width = width
        self.height = height
        self.unit = unit
        self.allow_nationwide_fallback = allow_nationwide_fallback
        self.nearby_max_pages = nearby_max_pages
        self.history_start_iso = history_start_iso
        self.history_end_iso = history_end_iso
        self.cache_dir = cache_dir
        self._nws_client = nws_client
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def _is_cancelled(self) -> bool:
        return self._cancel_requested

    def _check_cancelled(self) -> bool:
        if self._is_cancelled():
            self.fetch_log.emit("Fetch cancelled.")
            self.fetch_error.emit("Cancelled")
            return True
        return False

    def run(self) -> None:
        cache = CacheStore(self.cache_dir)
        client = self._nws_client or NWSClient()
        self.fetch_progress.emit(5, "Initializing")
        self.fetch_log.emit(
            f"Fetch start: mode={self.mode}, centroid=({self.latitude:.4f}, {self.longitude:.4f}), "
            f"size={self.width:.2f}x{self.height:.2f} {self.unit}, "
            f"nationwide_fallback={'on' if self.allow_nationwide_fallback else 'off'}"
        )

        if self._check_cancelled():
            return

        bbox = bbox_from_centroid(
            self.latitude,
            self.longitude,
            self.width,
            self.height,
            self.unit,
        )
        self.fetch_log.emit(
            f"Computed bbox: min_lat={bbox[0]:.6f}, min_lon={bbox[1]:.6f}, "
            f"max_lat={bbox[2]:.6f}, max_lon={bbox[3]:.6f}"
        )
        width_km = distance_to_km(self.width, self.unit)
        height_km = distance_to_km(self.height, self.unit)
        prefer_nearby = max(width_km, height_km) <= 250.0
        self.fetch_log.emit(
            f"Query span: {width_km:.2f}km x {height_km:.2f}km; "
            f"nearby-first lookup={'enabled' if prefer_nearby else 'disabled'}"
        )
        cache_key = self._cache_key(bbox)
        ttl = LIVE_CACHE_TTL_SECONDS if self.mode == "live" else HIST_CACHE_TTL_SECONDS
        self.fetch_log.emit(f"Cache lookup: ttl={ttl}s")

        if self._check_cancelled():
            return
        cached = cache.get(cache_key, max_age_seconds=ttl)
        if cached:
            self.fetch_log.emit("Cache hit: returning fresh cached payload.")
            payload, _ = cached
            data = payload.get("data", {})
            observations = _deserialize_observations(data.get("observations", []))
            historical_series = _deserialize_series(data.get("historical_series", {}))
            metadata = data.get("metadata", {})
            metadata["cache"] = "fresh"
            self.fetch_progress.emit(100, "Complete")
            self.fetch_success.emit(observations, historical_series, bbox, metadata)
            return

        stale = cache.get(cache_key, max_age_seconds=ttl, allow_stale=True)
        if stale:
            self.fetch_log.emit("Cache stale entry available for fallback.")
        else:
            self.fetch_log.emit("Cache miss: no reusable cached entry.")

        try:
            self.fetch_progress.emit(25, "Finding stations")
            self.fetch_log.emit("Requesting station list from NWS...")
            stations = client.get_stations_in_bbox(
                bbox,
                logger=self.fetch_log.emit,
                centroid=(self.latitude, self.longitude),
                prefer_nearby=prefer_nearby,
                allow_nationwide_fallback=self.allow_nationwide_fallback,
                nearby_max_pages=self.nearby_max_pages,
                should_cancel=self._is_cancelled,
            )
            if self._check_cancelled():
                return
            station_limit_reached = len(stations) >= MAX_STATIONS_PER_QUERY
            self.fetch_log.emit(
                f"Station query complete: station_count={len(stations)}, "
                f"limit_reached={station_limit_reached}"
            )
            if self.mode == "live":
                self.fetch_progress.emit(60, "Fetching latest observations")
                self.fetch_log.emit("Requesting latest observations...")
                observations = client.fetch_latest_observations(
                    stations,
                    logger=self.fetch_log.emit,
                    should_cancel=self._is_cancelled,
                )
                if self._check_cancelled():
                    return
                historical_series: Dict[str, List[WindStationObservation]] = {}
                metadata = {
                    "mode": "live",
                    "station_count": len(stations),
                    "observation_count": len(observations),
                    "history_point_count": 0,
                    "station_limit_reached": station_limit_reached,
                    "cache": "miss",
                }
                self.fetch_log.emit(
                    f"Latest observation fetch complete: observation_count={len(observations)}"
                )
            else:
                self.fetch_log.emit(
                    f"Requesting historical observations: start={self.history_start_iso}, "
                    f"end={self.history_end_iso}"
                )
                self.fetch_progress.emit(60, "Fetching historical observations")
                historical_series = client.fetch_historical_observations(
                    stations,
                    self.history_start_iso,
                    self.history_end_iso,
                    logger=self.fetch_log.emit,
                    should_cancel=self._is_cancelled,
                )
                if self._check_cancelled():
                    return
                observations = client.summarize_historical_latest(historical_series)
                point_count = sum(len(entries) for entries in historical_series.values())
                metadata = {
                    "mode": "historical",
                    "station_count": len(stations),
                    "observation_count": len(observations),
                    "history_point_count": point_count,
                    "station_limit_reached": station_limit_reached,
                    "history_start": self.history_start_iso,
                    "history_end": self.history_end_iso,
                    "cache": "miss",
                }
                self.fetch_log.emit(
                    f"Historical fetch complete: latest_count={len(observations)}, point_count={point_count}"
                )

            self.fetch_progress.emit(85, "Updating cache")
            cache.set(
                cache_key,
                {
                    "observations": [obs.to_dict() for obs in observations],
                    "historical_series": {
                        station_id: [entry.to_dict() for entry in entries]
                        for station_id, entries in historical_series.items()
                    },
                    "metadata": metadata,
                },
            )
            self.fetch_log.emit("Cache write complete.")
            self.fetch_progress.emit(100, "Complete")
            self.fetch_success.emit(observations, historical_series, bbox, metadata)
        except Exception as exc:
            self.fetch_log.emit(f"Fetch exception: {exc}")
            self.fetch_log.emit(traceback.format_exc().strip())
            if stale:
                self.fetch_log.emit("Falling back to stale cached payload.")
                payload, _ = stale
                data = payload.get("data", {})
                observations = _deserialize_observations(data.get("observations", []))
                historical_series = _deserialize_series(data.get("historical_series", {}))
                metadata = data.get("metadata", {})
                metadata["cache"] = "stale"
                metadata["warning"] = f"Network failed; showing stale cache: {exc}"
                self.fetch_progress.emit(100, "Complete (stale cache)")
                self.fetch_success.emit(observations, historical_series, bbox, metadata)
            else:
                self.fetch_log.emit("No stale cache available. Emitting fetch_error.")
                self.fetch_error.emit(str(exc))

    def _cache_key(self, bbox: tuple[float, float, float, float]) -> str:
        min_lat, min_lon, max_lat, max_lon = bbox
        base = (
            f"mode={self.mode}|"
            f"bbox={min_lat:.4f},{min_lon:.4f},{max_lat:.4f},{max_lon:.4f}|"
            f"unit={self.unit}"
        )
        if self.mode == "historical":
            base += f"|start={self.history_start_iso}|end={self.history_end_iso}"
        return base


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1480, 900)

        self._latest_results: List[WindStationObservation] = []
        self._historical_series: Dict[str, List[WindStationObservation]] = {}
        self._active_fetch_thread: FetchThread | None = None
        self._station_row_index: Dict[str, int] = {}
        self._last_bbox: tuple[float, float, float, float] | None = None
        self._last_query_metadata: dict = {}
        self._cache_store = CacheStore(CACHE_DIR)
        self._nws_client = NWSClient()
        self._settings = QSettings("WeatherWindApp", "USWindSensorExplorer")
        self._pending_refresh = False
        self._next_refresh_at: datetime | None = None
        self._programmatic_centroid_update = False
        self._is_closing = False
        self._current_stats: dict = {}
        self._last_fetch_stats: dict | None = None
        self._previous_fetch_stats: dict | None = None
        self._stats_dashboard: StatisticsDashboardDialog | None = None
        self._current_prevailing: dict | None = None
        self._prevailing_marker: tuple[float, float] | None = None

        self._build_ui()
        self._setup_timer()
        self._load_gui_settings()
        self._on_mode_changed(self.mode_combo.currentText())

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)

        left_panel = self._build_controls_panel()
        right_panel = self._build_data_panel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 1060])

        root_layout.addWidget(splitter)

    def _build_controls_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        query_group = QGroupBox("Query Area")
        query_form = QFormLayout(query_group)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Live", "Recent Historical"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)

        self.location_combo = QComboBox()
        self.location_combo.addItem("Custom")
        self.location_combo.addItems(list(USA_METRO_CENTROIDS.keys()))
        self.location_combo.currentTextChanged.connect(self._on_location_selected)

        self.lat_input = QDoubleSpinBox()
        self.lat_input.setRange(-90.0, 90.0)
        self.lat_input.setDecimals(6)
        self.lat_input.setValue(DEFAULT_CENTROID_LAT)
        self.lat_input.valueChanged.connect(self._on_centroid_manual_change)

        self.lon_input = QDoubleSpinBox()
        self.lon_input.setRange(-180.0, 180.0)
        self.lon_input.setDecimals(6)
        self.lon_input.setValue(DEFAULT_CENTROID_LON)
        self.lon_input.valueChanged.connect(self._on_centroid_manual_change)

        self.width_input = QDoubleSpinBox()
        self.width_input.setRange(1.0, 5000.0)
        self.width_input.setDecimals(2)
        self.width_input.setValue(DEFAULT_WIDTH)

        self.height_input = QDoubleSpinBox()
        self.height_input.setRange(1.0, 5000.0)
        self.height_input.setDecimals(2)
        self.height_input.setValue(DEFAULT_HEIGHT)

        self.dimension_unit_combo = QComboBox()
        self.dimension_unit_combo.addItems(list(SUPPORTED_DIMENSION_UNITS))
        self.dimension_unit_combo.setCurrentText(DEFAULT_DIMENSION_UNIT)

        self.speed_unit_combo = QComboBox()
        self.speed_unit_combo.addItems(["m/s", "mph", "kt"])
        self.speed_unit_combo.currentTextChanged.connect(self._refresh_presented_data)

        self.noaa_token_input = QLineEdit()
        self.noaa_token_input.setPlaceholderText("Paste NOAA CDO token here...")
        self.noaa_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.noaa_token_input.setText(self._settings.value("noaa_cdo_token", "", str))
        self.noaa_token_input.editingFinished.connect(self._save_gui_settings)
        self.noaa_token_input.textChanged.connect(self._update_noaa_token_status)

        self.noaa_token_status_label = QLabel()

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=DEFAULT_HISTORY_HOURS)

        self.history_start_input = QDateTimeEdit()
        self.history_start_input.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.history_start_input.setDateTime(QDateTime.fromSecsSinceEpoch(int(start.timestamp())))
        self.history_start_input.setCalendarPopup(True)

        self.history_end_input = QDateTimeEdit()
        self.history_end_input.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.history_end_input.setDateTime(QDateTime.fromSecsSinceEpoch(int(now.timestamp())))
        self.history_end_input.setCalendarPopup(True)

        query_form.addRow("Mode", self.mode_combo)
        query_form.addRow("Use Location", self.location_combo)
        query_form.addRow("Centroid Latitude", self.lat_input)
        query_form.addRow("Centroid Longitude", self.lon_input)
        query_form.addRow("Width", self.width_input)
        query_form.addRow("Height", self.height_input)
        query_form.addRow("Dimension Unit", self.dimension_unit_combo)
        query_form.addRow("Speed Unit", self.speed_unit_combo)
        query_form.addRow("NOAA CDO Token (Optional)", self.noaa_token_input)
        query_form.addRow("CDO Token Status", self.noaa_token_status_label)
        query_form.addRow("History Start (UTC)", self.history_start_input)
        query_form.addRow("History End (UTC)", self.history_end_input)

        schedule_group = QGroupBox("Automation")
        schedule_form = QFormLayout(schedule_group)

        self.auto_refresh_check = QCheckBox("Enable interval refresh")
        self.auto_refresh_check.toggled.connect(self._on_auto_refresh_toggled)

        self.interval_seconds_input = QSpinBox()
        self.interval_seconds_input.setRange(MIN_REFRESH_SECONDS, 86400)
        self.interval_seconds_input.setSingleStep(5)
        self.interval_seconds_input.setValue(DEFAULT_REFRESH_SECONDS)
        self.interval_seconds_input.valueChanged.connect(self._on_interval_changed)

        self.append_mode_check = QCheckBox("Append station snapshots")

        self.countdown_label = QLabel("Next refresh in: -")

        schedule_form.addRow(self.auto_refresh_check)
        schedule_form.addRow("Interval (sec)", self.interval_seconds_input)
        schedule_form.addRow(self.append_mode_check)
        schedule_form.addRow(self.countdown_label)

        export_group = QGroupBox("Export")
        export_layout = QVBoxLayout(export_group)

        export_csv_btn = QPushButton("Export CSV")
        export_csv_btn.clicked.connect(self._export_csv)
        export_json_btn = QPushButton("Export JSON")
        export_json_btn.clicked.connect(self._export_json)
        export_kml_btn = QPushButton("Export KML/KMZ")
        export_kml_btn.setToolTip("KMZ is recommended for Google Earth compatibility with arrow icons.")
        export_kml_btn.clicked.connect(self._export_kml)
        clear_cache_btn = QPushButton("Clear Cache")
        clear_cache_btn.clicked.connect(self._clear_cache)

        export_layout.addWidget(export_csv_btn)
        export_layout.addWidget(export_json_btn)
        export_layout.addWidget(export_kml_btn)
        export_layout.addWidget(clear_cache_btn)

        map_group = QGroupBox("Map")
        map_layout = QVBoxLayout(map_group)
        self.wind_vector_mode_combo = QComboBox()
        self.wind_vector_mode_combo.addItems(["Upwind (from)", "Downwind (to)"])
        self.wind_vector_mode_combo.currentTextChanged.connect(self._on_wind_vector_mode_changed)
        go_to_bbox_btn = QPushButton("Go To Bounding Box")
        go_to_bbox_btn.clicked.connect(self._go_to_bounding_box)
        map_layout.addWidget(QLabel("Arrow Direction"))
        map_layout.addWidget(self.wind_vector_mode_combo)
        map_layout.addWidget(go_to_bbox_btn)

        advanced_group = QGroupBox("Advanced")
        advanced_form = QFormLayout(advanced_group)

        self.nearby_max_pages_input = QSpinBox()
        self.nearby_max_pages_input.setRange(MIN_NEARBY_MAX_PAGES, MAX_NEARBY_MAX_PAGES)
        self.nearby_max_pages_input.setValue(DEFAULT_NEARBY_MAX_PAGES)

        self.nationwide_fallback_check = QCheckBox("Allow slow nationwide fallback")
        self.nationwide_fallback_check.setChecked(False)

        self.noaa_token_input.setToolTip(
            "NOAA CDO token for extended historical data (Phase 4). "
            "Current live and NWS historical fetches work without it."
        )

        advanced_form.addRow("Nearby Page Cap", self.nearby_max_pages_input)
        advanced_form.addRow(self.nationwide_fallback_check)

        tabs = QTabWidget()
        query_tab = QWidget()
        query_tab_layout = QVBoxLayout(query_tab)
        query_tab_layout.addWidget(query_group)
        query_tab_layout.addStretch(1)

        automation_tab = QWidget()
        automation_tab_layout = QVBoxLayout(automation_tab)
        automation_tab_layout.addWidget(schedule_group)
        automation_tab_layout.addStretch(1)

        export_tab = QWidget()
        export_tab_layout = QVBoxLayout(export_tab)
        export_tab_layout.addWidget(export_group)
        export_tab_layout.addStretch(1)

        map_tab = QWidget()
        map_tab_layout = QVBoxLayout(map_tab)
        map_tab_layout.addWidget(map_group)
        map_tab_layout.addWidget(advanced_group)
        map_tab_layout.addStretch(1)

        prevailing_tab = QWidget()
        prevailing_tab_layout = QVBoxLayout(prevailing_tab)
        prevailing_group = QGroupBox("Prevailing Wind")
        prevailing_form = QFormLayout(prevailing_group)

        self.prevailing_speed_label = QLabel("n/a")
        self.prevailing_direction_label = QLabel("n/a")
        self.prevailing_compass_label = QLabel("n/a")
        self.prevailing_used_label = QLabel("0 / 0")
        self.prevailing_confidence_label = QLabel("n/a")
        self.prevailing_confidence_level_label = QLabel("n/a")
        self.prevailing_confidence_legend_label = QLabel("Legend: Low < 0.40, Moderate 0.40-0.70, High > 0.70")
        self.prevailing_confidence_legend_label.setWordWrap(True)
        self.prevailing_weight_mode_label = QLabel("Uniform")
        self.prevailing_marker_status_label = QLabel("No marker placed")
        self.prevailing_marker_status_label.setWordWrap(True)
        self.prevailing_use_marker_check = QCheckBox("Use marker weighting")
        self.prevailing_use_marker_check.setChecked(False)
        self.prevailing_use_marker_check.toggled.connect(self._on_prevailing_overlay_toggled)
        self.prevailing_place_marker_check = QCheckBox("Enable marker placement on map")
        self.prevailing_place_marker_check.setChecked(False)
        self.prevailing_place_marker_check.toggled.connect(self._on_prevailing_marker_placement_toggled)
        self.prevailing_buffer_ring_input = QDoubleSpinBox()
        self.prevailing_buffer_ring_input.setRange(0.0, 5000.0)
        self.prevailing_buffer_ring_input.setDecimals(1)
        self.prevailing_buffer_ring_input.setSingleStep(1.0)
        self.prevailing_buffer_ring_input.setValue(10.0)
        self.prevailing_buffer_ring_input.valueChanged.connect(self._on_prevailing_weight_input_changed)
        self.prevailing_buffer_ring_label = QLabel("Buffer Ring")
        self.prevailing_weight_curve_input = QDoubleSpinBox()
        self.prevailing_weight_curve_input.setRange(0.0, 4.0)
        self.prevailing_weight_curve_input.setDecimals(1)
        self.prevailing_weight_curve_input.setSingleStep(0.1)
        self.prevailing_weight_curve_input.setValue(1.0)
        self.prevailing_weight_curve_input.valueChanged.connect(self._on_prevailing_weight_input_changed)
        self.prevailing_clear_marker_btn = QPushButton("Clear marker")
        self.prevailing_clear_marker_btn.clicked.connect(self._clear_prevailing_marker)
        self.prevailing_show_arrow_check = QCheckBox("Show arrow on map")
        self.prevailing_show_text_check = QCheckBox("Show text on map")
        self.prevailing_show_arrow_check.setChecked(False)
        self.prevailing_show_text_check.setChecked(False)
        self.prevailing_show_arrow_check.toggled.connect(self._on_prevailing_overlay_toggled)
        self.prevailing_show_text_check.toggled.connect(self._on_prevailing_overlay_toggled)

        prevailing_note = QLabel(
            "Calculated from currently visible stations with speed and direction values."
        )
        prevailing_note.setWordWrap(True)
        prevailing_marker_help = QLabel(
            "How to drop marker: 1) Click Go To Bounding Box, 2) Enable marker placement on map, 3) Click once inside the blue bbox on the map."
        )
        prevailing_marker_help.setWordWrap(True)
        prevailing_weight_help = QLabel(
            "Smooth weight function (when marker weighting is enabled): w = 1 / (1 + d / r)^p, where d is distance to marker, r is buffer ring, and p is Weight Curve."
        )
        prevailing_weight_help.setWordWrap(True)

        prevailing_form.addRow("Speed", self.prevailing_speed_label)
        prevailing_form.addRow("Direction", self.prevailing_direction_label)
        prevailing_form.addRow("Compass", self.prevailing_compass_label)
        prevailing_form.addRow("Stations Used", self.prevailing_used_label)
        prevailing_form.addRow("Consistency", self.prevailing_confidence_label)
        prevailing_form.addRow("Consistency Level", self.prevailing_confidence_level_label)
        prevailing_form.addRow(self.prevailing_confidence_legend_label)
        prevailing_form.addRow("Weight Mode", self.prevailing_weight_mode_label)
        prevailing_form.addRow("Marker", self.prevailing_marker_status_label)
        prevailing_form.addRow(self.prevailing_buffer_ring_label, self.prevailing_buffer_ring_input)
        prevailing_form.addRow("Weight Curve (p)", self.prevailing_weight_curve_input)
        prevailing_form.addRow(self.prevailing_use_marker_check)
        prevailing_form.addRow(self.prevailing_place_marker_check)
        prevailing_form.addRow(self.prevailing_clear_marker_btn)
        prevailing_form.addRow(self.prevailing_show_arrow_check)
        prevailing_form.addRow(self.prevailing_show_text_check)
        prevailing_form.addRow(prevailing_weight_help)
        prevailing_form.addRow(prevailing_marker_help)
        prevailing_form.addRow(prevailing_note)

        self.dimension_unit_combo.currentTextChanged.connect(self._on_dimension_unit_changed)
        self._update_prevailing_unit_labels()

        prevailing_tab_layout.addWidget(prevailing_group)
        prevailing_tab_layout.addStretch(1)

        tabs.addTab(query_tab, "Query")
        tabs.addTab(automation_tab, "Automation")
        tabs.addTab(export_tab, "Export")
        tabs.addTab(map_tab, "Map/Advanced")
        tabs.addTab(prevailing_tab, "Prevailing Wind")

        self.fetch_progress_bar = QProgressBar()
        self.fetch_progress_bar.setRange(0, 100)
        self.fetch_progress_bar.setValue(0)
        self.fetch_progress_bar.setFormat("Idle")

        self.fetch_button = QPushButton("Fetch Now")
        self.fetch_button.clicked.connect(self._fetch_now)

        self.exit_button = QPushButton("Exit Application")
        self.exit_button.clicked.connect(self.close)

        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)

        layout.addWidget(tabs)
        layout.addWidget(self.fetch_progress_bar)
        layout.addWidget(self.fetch_button)
        layout.addWidget(self.exit_button)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        return container

    def _build_data_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self.map_view = QWebEngineView()
        web_settings = self.map_view.settings()
        web_settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            True,
        )
        web_settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            True,
        )
        self.map_view.load(QUrl.fromLocalFile(str(MAP_HTML_PATH)))
        self.map_view.loadFinished.connect(self._on_map_loaded)

        self.map_bridge = MapBridge()
        self.map_bridge.station_clicked.connect(self._on_station_clicked)
        self.map_bridge.prevailing_marker_placed.connect(self._on_prevailing_marker_placed)
        self.map_bridge.prevailing_marker_rejected.connect(self._on_prevailing_marker_rejected)

        channel = QWebChannel(self.map_view.page())
        channel.registerObject("bridge", self.map_bridge)
        self.map_view.page().setWebChannel(channel)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            [
                "Station ID",
                "Name",
                "Lat",
                "Lon",
                "Speed",
                "Gust",
                "Direction",
                "Updated",
                "Lapsed Since Update",
                "Source",
            ]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)

        table_filter_panel = QWidget()
        table_filter_layout = QHBoxLayout(table_filter_panel)
        table_filter_layout.setContentsMargins(0, 0, 0, 0)
        self.table_filter_input = QLineEdit()
        self.table_filter_input.setPlaceholderText("Filter by station id or name")
        self.table_filter_input.textChanged.connect(self._apply_table_filters)
        self.source_filter_combo = QComboBox()
        self.source_filter_combo.addItems(["All Sources", "NWS", "NWS-HIST"])
        self.source_filter_combo.currentTextChanged.connect(self._apply_table_filters)
        self.with_speed_only_check = QCheckBox("Only With Speed")
        self.with_speed_only_check.toggled.connect(self._apply_table_filters)
        self.with_gust_only_check = QCheckBox("Only With Gust")
        self.with_gust_only_check.toggled.connect(self._apply_table_filters)
        self.with_direction_only_check = QCheckBox("Only With Direction")
        self.with_direction_only_check.toggled.connect(self._apply_table_filters)
        self.hide_stale_timestamp_check = QCheckBox("Hide Stale Timestamp")
        self.hide_stale_timestamp_check.toggled.connect(self._apply_table_filters)
        self.stale_minutes_input = QSpinBox()
        self.stale_minutes_input.setRange(1, 7 * 24 * 60)
        self.stale_minutes_input.setValue(60)
        self.stale_minutes_input.setSuffix("m")
        self.stale_minutes_input.setToolTip("Rows older than this threshold are considered stale.")
        self.stale_minutes_input.valueChanged.connect(self._apply_table_filters)
        table_filter_layout.addWidget(self.table_filter_input)
        table_filter_layout.addWidget(self.source_filter_combo)
        table_filter_layout.addWidget(self.with_speed_only_check)
        table_filter_layout.addWidget(self.with_gust_only_check)
        table_filter_layout.addWidget(self.with_direction_only_check)
        table_filter_layout.addWidget(self.hide_stale_timestamp_check)
        table_filter_layout.addWidget(self.stale_minutes_input)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Fetch logs will appear here...")

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_button_row = QHBoxLayout()

        copy_log_btn = QPushButton("Copy Logs")
        copy_log_btn.clicked.connect(self._copy_logs)
        save_log_btn = QPushButton("Save Logs")
        save_log_btn.clicked.connect(self._save_logs)
        clear_log_btn = QPushButton("Clear Logs")
        clear_log_btn.clicked.connect(self.log_output.clear)

        log_button_row.addWidget(copy_log_btn)
        log_button_row.addWidget(save_log_btn)
        log_button_row.addWidget(clear_log_btn)
        log_button_row.addStretch(1)

        log_layout.addLayout(log_button_row)
        log_layout.addWidget(self.log_output)

        stats_panel = QWidget()
        stats_layout = QVBoxLayout(stats_panel)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_button_row = QHBoxLayout()

        recompute_stats_btn = QPushButton("Recompute Statistics")
        recompute_stats_btn.clicked.connect(self._recompute_statistics)
        copy_stats_btn = QPushButton("Copy Statistics")
        copy_stats_btn.clicked.connect(self._copy_statistics_summary)
        export_stats_btn = QPushButton("Export Statistics JSON")
        export_stats_btn.clicked.connect(self._export_statistics_json)
        dashboard_stats_btn = QPushButton("View Statistics Dashboard")
        dashboard_stats_btn.clicked.connect(self._open_statistics_dashboard)

        stats_button_row.addWidget(recompute_stats_btn)
        stats_button_row.addWidget(copy_stats_btn)
        stats_button_row.addWidget(export_stats_btn)
        stats_button_row.addWidget(dashboard_stats_btn)
        stats_button_row.addStretch(1)

        self.stats_output = QTextEdit()
        self.stats_output.setReadOnly(True)
        self.stats_output.setPlaceholderText("Computed statistics will appear here...")

        stats_layout.addLayout(stats_button_row)
        stats_layout.addWidget(self.stats_output)

        lower_tabs = QTabWidget()
        lower_tabs.addTab(log_panel, "Logs")
        lower_tabs.addTab(stats_panel, "Computed Statistics")

        bottom_panel = QWidget()
        bottom_layout = QVBoxLayout(bottom_panel)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(6)

        bottom_splitter = QSplitter(Qt.Orientation.Vertical)
        bottom_splitter.addWidget(self.table)
        bottom_splitter.addWidget(lower_tabs)
        bottom_splitter.setStretchFactor(0, 3)
        bottom_splitter.setStretchFactor(1, 2)

        bottom_layout.addWidget(table_filter_panel)
        bottom_layout.addWidget(bottom_splitter)

        data_splitter = QSplitter(Qt.Orientation.Vertical)
        data_splitter.addWidget(self.map_view)
        data_splitter.addWidget(bottom_panel)
        data_splitter.setStretchFactor(0, 3)
        data_splitter.setStretchFactor(1, 3)

        layout.addWidget(data_splitter)
        return container

    def _setup_timer(self) -> None:
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._fetch_now)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._update_countdown)
        self.lapsed_timer = QTimer(self)
        self.lapsed_timer.setInterval(1000)
        self.lapsed_timer.timeout.connect(self._update_lapsed_column)
        self.lapsed_timer.start()
        self._on_interval_changed(self.interval_seconds_input.value())

    @Slot()
    def _fetch_now(self) -> None:
        if self._is_closing:
            return
        self._save_gui_settings()
        self._append_log("Fetch requested.")
        if self._active_fetch_thread is not None and self._active_fetch_thread.isRunning():
            self._pending_refresh = True
            self.status_label.setText("Fetch in progress; queued one refresh.")
            self._append_log("Fetch already in progress. Queued one follow-up fetch.")
            return

        if self.mode_combo.currentIndex() == 1:
            start = self._history_start_iso()
            end = self._history_end_iso()
            if start >= end:
                QMessageBox.warning(self, "Invalid Range", "History start must be earlier than end.")
                self._append_log("Fetch aborted: invalid historical date range.")
                return
        else:
            start = ""
            end = ""

        self.fetch_button.setEnabled(False)
        self._set_progress(0, "Starting")
        mode_text = "live" if self.mode_combo.currentIndex() == 0 else "historical"
        self.status_label.setText(f"Fetching {mode_text} station observations...")
        self._append_log(f"Starting fetch thread in {mode_text} mode.")

        self._active_fetch_thread = FetchThread(
            mode=mode_text,
            latitude=self.lat_input.value(),
            longitude=self.lon_input.value(),
            width=self.width_input.value(),
            height=self.height_input.value(),
            unit=self.dimension_unit_combo.currentText(),
            allow_nationwide_fallback=self.nationwide_fallback_check.isChecked(),
            nearby_max_pages=self.nearby_max_pages_input.value(),
            history_start_iso=start,
            history_end_iso=end,
            cache_dir=CACHE_DIR,
            nws_client=self._nws_client,
        )
        self._active_fetch_thread.fetch_success.connect(self._on_fetch_success)
        self._active_fetch_thread.fetch_error.connect(self._on_fetch_error)
        self._active_fetch_thread.fetch_log.connect(self._append_log)
        self._active_fetch_thread.fetch_progress.connect(self._on_fetch_progress)
        self._active_fetch_thread.finished.connect(self._on_fetch_finished)
        self._active_fetch_thread.start()

    @Slot(object, object, object, object)
    def _on_fetch_success(
        self,
        observations: object,
        historical_series: object,
        bbox: object,
        metadata: object,
    ) -> None:
        new_observations = list(observations)
        incoming_series = dict(historical_series) if isinstance(historical_series, dict) else {}

        if self.append_mode_check.isChecked() and self._latest_results:
            self._latest_results = self._merge_observation_snapshots(self._latest_results, new_observations)
            self._historical_series = self._merge_historical_series(self._historical_series, incoming_series)
        else:
            self._latest_results = new_observations
            self._historical_series = incoming_series

        self._last_query_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        self._last_bbox = bbox if isinstance(bbox, tuple) and len(bbox) == 4 else None
        self._refresh_presented_data()
        if self._current_stats:
            self._previous_fetch_stats = dict(self._last_fetch_stats) if self._last_fetch_stats else None
            self._last_fetch_stats = dict(self._current_stats)
            self._render_statistics()

        if self._last_bbox:
            min_lat, min_lon, max_lat, max_lon = self._last_bbox
            self.map_view.page().runJavaScript(
                (
                    "if (typeof window.setQueryBounds === 'function') {"
                    f"window.setQueryBounds({min_lat}, {min_lon}, {max_lat}, {max_lon});"
                    "}"
                )
            )

        stations = self._last_query_metadata.get("station_count", 0)
        observations_count = self._last_query_metadata.get("observation_count", 0)
        cache_state = self._last_query_metadata.get("cache", "n/a")
        point_count = self._last_query_metadata.get("history_point_count", 0)
        capped = self._last_query_metadata.get("station_limit_reached", False)
        warning = self._last_query_metadata.get("warning")
        base = (
            f"Loaded {observations_count} observations from {stations} stations "
            f"(history points: {point_count}, cache: {cache_state})."
        )
        if capped:
            base += " Station list hit current cap; narrow the area for complete coverage."
        if warning:
            base += f" {warning}"
        self.status_label.setText(base)
        self._append_log(base)

    @Slot(str)
    def _on_fetch_error(self, message: str) -> None:
        self.status_label.setText(f"Fetch failed: {message}")
        self._append_log(f"Fetch failed: {message}")
        self._set_progress(0, "Idle")
        if message != "Cancelled":
            QMessageBox.warning(self, "Fetch Error", message)

    @Slot()
    def _on_fetch_finished(self) -> None:
        self.fetch_button.setEnabled(True)
        self._set_progress(100, "Done")
        self._append_log("Fetch thread finished.")
        self._active_fetch_thread = None
        if self._is_closing:
            return
        if self.auto_refresh_check.isChecked():
            self._schedule_next_refresh()
        if self._pending_refresh:
            self._pending_refresh = False
            self._fetch_now()

    @Slot()
    def _go_to_bounding_box(self) -> None:
        bbox = bbox_from_centroid(
            self.lat_input.value(),
            self.lon_input.value(),
            self.width_input.value(),
            self.height_input.value(),
            self.dimension_unit_combo.currentText(),
        )
        min_lat, min_lon, max_lat, max_lon = bbox
        self.map_view.page().runJavaScript(
            (
                "if (typeof window.setQueryBounds === 'function') {"
                f"window.setQueryBounds({min_lat}, {min_lon}, {max_lat}, {max_lon});"
                "}"
            )
        )
        self._last_bbox = bbox
        self._append_log(
            "Go To Bounding Box: "
            f"min_lat={min_lat:.6f}, min_lon={min_lon:.6f}, "
            f"max_lat={max_lat:.6f}, max_lon={max_lon:.6f}"
        )
        self._validate_prevailing_marker_within_bbox()
        self._push_prevailing_overlay()
        self._push_prevailing_marker()

    @Slot(str)
    def _on_mode_changed(self, _: str) -> None:
        historical = self.mode_combo.currentIndex() == 1
        self.history_start_input.setEnabled(historical)
        self.history_end_input.setEnabled(historical)
        self.status_label.setText("Historical mode ready." if historical else "Live mode ready.")

    @Slot(str)
    def _on_wind_vector_mode_changed(self, _: str) -> None:
        self._save_gui_settings()
        self._refresh_presented_data()

    @Slot(str)
    def _on_dimension_unit_changed(self, _: str) -> None:
        self._update_prevailing_unit_labels()
        self._save_gui_settings()
        self._refresh_presented_data()

    @Slot(bool)
    def _on_prevailing_overlay_toggled(self, _: bool) -> None:
        self._save_gui_settings()
        self._refresh_presented_data()

    @Slot(bool)
    def _on_map_loaded(self, loaded: bool) -> None:
        if not loaded:
            return
        self._push_prevailing_marker()
        self._push_prevailing_overlay()

    @Slot(float)
    def _on_prevailing_weight_input_changed(self, _: float) -> None:
        self._save_gui_settings()
        self._refresh_presented_data()

    @Slot(bool)
    def _on_prevailing_marker_placement_toggled(self, enabled: bool) -> None:
        self._save_gui_settings()
        self.map_view.page().runJavaScript(
            f"if (typeof window.setPrevailingMarkerPlacement === 'function') {{ window.setPrevailingMarkerPlacement({str(enabled).lower()}); }}"
        )
        if enabled:
            self.status_label.setText("Prevailing marker placement enabled. Click inside the bbox to place a marker.")
        else:
            self.status_label.setText("Prevailing marker placement disabled.")
        self._push_prevailing_marker()

    @Slot(float, float)
    def _on_prevailing_marker_placed(self, latitude: float, longitude: float) -> None:
        if self._last_bbox is None:
            self._last_bbox = bbox_from_centroid(
                self.lat_input.value(),
                self.lon_input.value(),
                self.width_input.value(),
                self.height_input.value(),
                self.dimension_unit_combo.currentText(),
            )
        min_lat, min_lon, max_lat, max_lon = self._last_bbox
        if not point_in_bbox(latitude, longitude, min_lat, min_lon, max_lat, max_lon):
            self._reject_prevailing_marker("Marker must be placed inside the current bounding box.")
            return
        self._prevailing_marker = (latitude, longitude)
        self._save_gui_settings()
        self._refresh_presented_data()

    @Slot(str)
    def _on_prevailing_marker_rejected(self, message: str) -> None:
        self._reject_prevailing_marker(message)

    def _reject_prevailing_marker(self, message: str) -> None:
        self._prevailing_marker = None
        self.prevailing_marker_status_label.setText("No marker placed")
        self.status_label.setText(message)
        self._save_gui_settings()
        self._push_prevailing_marker()

    def _clear_prevailing_marker(self) -> None:
        self._prevailing_marker = None
        self.prevailing_marker_status_label.setText("No marker placed")
        self._save_gui_settings()
        self.map_view.page().runJavaScript(
            "if (typeof window.clearPrevailingMarker === 'function') { window.clearPrevailingMarker(); }"
        )
        self._refresh_presented_data()

    def _validate_prevailing_marker_within_bbox(self) -> None:
        if self._prevailing_marker is None or self._last_bbox is None:
            return
        min_lat, min_lon, max_lat, max_lon = self._last_bbox
        if not point_in_bbox(self._prevailing_marker[0], self._prevailing_marker[1], min_lat, min_lon, max_lat, max_lon):
            self._reject_prevailing_marker("Marker was outside the current bounding box and was cleared.")

    def _push_prevailing_marker(self) -> None:
        payload = {
            "latitude": self._prevailing_marker[0] if self._prevailing_marker else None,
            "longitude": self._prevailing_marker[1] if self._prevailing_marker else None,
            "enabled": self.prevailing_place_marker_check.isChecked(),
        }
        self.map_view.page().runJavaScript(
            f"if (typeof window.setPrevailingMarker === 'function') {{ window.setPrevailingMarker({json.dumps(payload)}); }}"
        )

    def _update_prevailing_unit_labels(self) -> None:
        unit = self.dimension_unit_combo.currentText()
        self.prevailing_buffer_ring_label.setText(f"Buffer Ring ({unit})")
        self.prevailing_buffer_ring_input.setSuffix(f" {unit}")

    @Slot(str)
    def _on_location_selected(self, location_name: str) -> None:
        if location_name == "Custom":
            return
        coords = USA_METRO_CENTROIDS.get(location_name)
        if coords is None:
            return
        self._programmatic_centroid_update = True
        try:
            self.lat_input.setValue(coords[0])
            self.lon_input.setValue(coords[1])
        finally:
            self._programmatic_centroid_update = False

    @Slot(float)
    def _on_centroid_manual_change(self, _: float) -> None:
        if self._programmatic_centroid_update:
            return
        if self.location_combo.currentText() != "Custom":
            self.location_combo.blockSignals(True)
            self.location_combo.setCurrentText("Custom")
            self.location_combo.blockSignals(False)

    @Slot(bool)
    def _on_auto_refresh_toggled(self, enabled: bool) -> None:
        if enabled:
            self._schedule_next_refresh()
            self.countdown_timer.start()
            self.status_label.setText("Auto-refresh enabled.")
        else:
            self.refresh_timer.stop()
            self.countdown_timer.stop()
            self._next_refresh_at = None
            self.countdown_label.setText("Next refresh in: -")
            self.status_label.setText("Auto-refresh disabled.")

    @Slot(int)
    def _on_interval_changed(self, seconds: int) -> None:
        seconds = max(MIN_REFRESH_SECONDS, seconds)
        self.interval_seconds_input.blockSignals(True)
        self.interval_seconds_input.setValue(seconds)
        self.interval_seconds_input.blockSignals(False)
        self.refresh_timer.setInterval(seconds * 1000)
        if self.auto_refresh_check.isChecked():
            self._schedule_next_refresh()

    @Slot(str)
    def _on_station_clicked(self, station_id: str) -> None:
        target_row = None
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == station_id:
                target_row = row
                break
        if target_row is None:
            return
        self.table.selectRow(target_row)
        self.table.scrollToItem(self.table.item(target_row, 0))

    @Slot()
    def _refresh_presented_data(self) -> None:
        speed_unit = self.speed_unit_combo.currentText()
        self._station_row_index.clear()

        was_sorting_enabled = self.table.isSortingEnabled()
        if was_sorting_enabled:
            self.table.setSortingEnabled(False)

        self.table.setRowCount(len(self._latest_results))
        map_payload = []

        for row, obs in enumerate(self._latest_results):
            speed = obs.speed_for_display(speed_unit)
            gust = obs.gust_for_display(speed_unit)
            direction_label = obs.direction_arrow()
            updated = obs.timestamp.isoformat() if obs.timestamp else "n/a"
            lapsed_text = _format_lapsed_since(obs.timestamp)
            lapsed_seconds = _lapsed_seconds(obs.timestamp)
            updated_epoch = _timestamp_epoch_seconds(obs.timestamp)

            id_item = self._set_table_item(row, 0, obs.station_id, sort_value=obs.station_id)
            self._set_table_item(row, 1, obs.station_name, sort_value=obs.station_name)
            self._set_table_item(row, 2, f"{obs.latitude:.4f}", sort_value=obs.latitude)
            self._set_table_item(row, 3, f"{obs.longitude:.4f}", sort_value=obs.longitude)
            self._set_table_item(row, 4, _fmt_numeric(speed), sort_value=_sort_numeric_or_none(speed))
            self._set_table_item(row, 5, _fmt_numeric(gust), sort_value=_sort_numeric_or_none(gust))
            self._set_table_item(
                row,
                6,
                direction_label,
                sort_value=_sort_numeric_or_none(obs.wind_direction_deg),
            )
            self._set_table_item(row, 7, updated, sort_value=updated_epoch)
            self._set_table_item(row, 8, lapsed_text, sort_value=lapsed_seconds)
            self._set_table_item(row, 9, obs.source, sort_value=obs.source)

            map_entry = {
                "station_id": obs.station_id,
                "station_name": obs.station_name,
                "latitude": obs.latitude,
                "longitude": obs.longitude,
                "wind_speed_mps": obs.wind_speed_mps,
                "wind_direction_deg": obs.wind_direction_deg,
                "speed_display": speed,
                "gust_display": gust,
                "speed_unit": speed_unit,
                "timestamp": updated,
                "lapsed_since_update": lapsed_text,
            }
            id_item.setData(
                STATION_META_ROLE,
                {
                    "station_id": obs.station_id,
                    "station_name": obs.station_name,
                    "station_name_lc": obs.station_name.lower(),
                    "latitude": obs.latitude,
                    "longitude": obs.longitude,
                    "source": obs.source,
                    "wind_speed_mps": obs.wind_speed_mps,
                    "wind_gust_mps": obs.wind_gust_mps,
                    "wind_direction_deg": obs.wind_direction_deg,
                    "timestamp_epoch": updated_epoch,
                    "lapsed_seconds": lapsed_seconds,
                    "speed_display": speed,
                    "gust_display": gust,
                    "map_entry": map_entry,
                },
            )

            self._station_row_index[obs.station_id] = row
            map_payload.append(map_entry)

        self.table.resizeColumnsToContents()
        if was_sorting_enabled:
            self.table.setSortingEnabled(True)
        payload_json = json.dumps(map_payload)
        self.map_view.page().runJavaScript(
            f"if (typeof window.setStations === 'function') {{ window.setStations({payload_json}); }}"
        )
        mode = "downwind" if self.wind_vector_mode_combo.currentText().startswith("Downwind") else "upwind"
        self.map_view.page().runJavaScript(
            f"if (typeof window.setWindVectorMode === 'function') {{ window.setWindVectorMode('{mode}'); }}"
        )
        self._apply_table_filters()

    @Slot()
    def _export_csv(self) -> None:
        if not self._latest_results:
            QMessageBox.information(self, "Nothing to Export", "Fetch data first.")
            return
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            "wind_export.csv",
            "CSV (*.csv)",
        )
        if not out:
            return
        try:
            export_csv(
                Path(out),
                self._latest_results,
                self._historical_series,
                self.speed_unit_combo.currentText(),
            )
            self.status_label.setText(f"CSV exported: {out}")
        except Exception as exc:
            self.status_label.setText("CSV export failed.")
            QMessageBox.critical(self, "Export Error", f"CSV export failed: {exc}")

    @Slot()
    def _export_json(self) -> None:
        if not self._latest_results:
            QMessageBox.information(self, "Nothing to Export", "Fetch data first.")
            return
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Export JSON",
            "wind_export.json",
            "JSON (*.json)",
        )
        if not out:
            return
        try:
            export_json(
                Path(out),
                self._latest_results,
                self._historical_series,
                self._export_metadata(),
            )
            self.status_label.setText(f"JSON exported: {out}")
        except Exception as exc:
            self.status_label.setText("JSON export failed.")
            QMessageBox.critical(self, "Export Error", f"JSON export failed: {exc}")

    @Slot()
    def _export_kml(self) -> None:
        if not self._latest_results:
            QMessageBox.information(self, "Nothing to Export", "Fetch data first.")
            return
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Export KML/KMZ (KMZ recommended for Google Earth)",
            "wind_export.kmz",
            "KML/KMZ (*.kml *.kmz)",
        )
        if not out:
            return
        try:
            vector_mode = "downwind" if self.wind_vector_mode_combo.currentText().startswith("Downwind") else "upwind"
            export_kml(
                Path(out),
                self._latest_results,
                self.speed_unit_combo.currentText(),
                self._export_metadata(),
                wind_vector_mode=vector_mode,
            )
            self.status_label.setText(f"KML/KMZ exported: {out}")
        except Exception as exc:
            self.status_label.setText("KML/KMZ export failed.")
            QMessageBox.critical(self, "Export Error", f"KML/KMZ export failed: {exc}")

    @Slot()
    def _clear_cache(self) -> None:
        self._cache_store.clear()
        self.status_label.setText("Cache cleared.")

    @Slot()
    def _save_gui_settings(self) -> None:
        self._settings.setValue("mode", self.mode_combo.currentText())
        self._settings.setValue("location", self.location_combo.currentText())
        self._settings.setValue("lat", self.lat_input.value())
        self._settings.setValue("lon", self.lon_input.value())
        self._settings.setValue("width", self.width_input.value())
        self._settings.setValue("height", self.height_input.value())
        self._settings.setValue("dimension_unit", self.dimension_unit_combo.currentText())
        self._settings.setValue("speed_unit", self.speed_unit_combo.currentText())
        self._settings.setValue("history_start", self.history_start_input.dateTime().toString(Qt.DateFormat.ISODate))
        self._settings.setValue("history_end", self.history_end_input.dateTime().toString(Qt.DateFormat.ISODate))
        self._settings.setValue("auto_refresh", self.auto_refresh_check.isChecked())
        self._settings.setValue("interval_seconds", self.interval_seconds_input.value())
        self._settings.setValue("append_mode", self.append_mode_check.isChecked())
        self._settings.setValue("nationwide_fallback", self.nationwide_fallback_check.isChecked())
        self._settings.setValue("noaa_cdo_token", self.noaa_token_input.text().strip())
        self._settings.setValue("nearby_max_pages", self.nearby_max_pages_input.value())
        self._settings.setValue("wind_vector_mode", self.wind_vector_mode_combo.currentText())
        self._settings.setValue("prevailing_use_marker", self.prevailing_use_marker_check.isChecked())
        self._settings.setValue("prevailing_marker_placement", self.prevailing_place_marker_check.isChecked())
        self._settings.setValue("prevailing_buffer_ring", self.prevailing_buffer_ring_input.value())
        self._settings.setValue("prevailing_weight_curve", self.prevailing_weight_curve_input.value())
        if self._prevailing_marker is None:
            self._settings.remove("prevailing_marker_lat")
            self._settings.remove("prevailing_marker_lon")
        else:
            self._settings.setValue("prevailing_marker_lat", self._prevailing_marker[0])
            self._settings.setValue("prevailing_marker_lon", self._prevailing_marker[1])
        self._settings.setValue("prevailing_show_arrow", self.prevailing_show_arrow_check.isChecked())
        self._settings.setValue("prevailing_show_text", self.prevailing_show_text_check.isChecked())

    def _load_gui_settings(self) -> None:
        mode = self._settings.value("mode", self.mode_combo.currentText(), str)
        self.mode_combo.setCurrentText(mode)

        location = self._settings.value("location", self.location_combo.currentText(), str)
        if self.location_combo.findText(location) >= 0:
            self.location_combo.setCurrentText(location)

        self.lat_input.setValue(float(self._settings.value("lat", self.lat_input.value(), float)))
        self.lon_input.setValue(float(self._settings.value("lon", self.lon_input.value(), float)))
        self.width_input.setValue(float(self._settings.value("width", self.width_input.value(), float)))
        self.height_input.setValue(float(self._settings.value("height", self.height_input.value(), float)))

        dimension_unit = self._settings.value("dimension_unit", self.dimension_unit_combo.currentText(), str)
        if self.dimension_unit_combo.findText(dimension_unit) >= 0:
            self.dimension_unit_combo.setCurrentText(dimension_unit)

        speed_unit = self._settings.value("speed_unit", self.speed_unit_combo.currentText(), str)
        if self.speed_unit_combo.findText(speed_unit) >= 0:
            self.speed_unit_combo.setCurrentText(speed_unit)

        history_start = self._settings.value("history_start", "", str)
        history_end = self._settings.value("history_end", "", str)
        if history_start:
            parsed = QDateTime.fromString(history_start, Qt.DateFormat.ISODate)
            if parsed.isValid():
                self.history_start_input.setDateTime(parsed)
        if history_end:
            parsed = QDateTime.fromString(history_end, Qt.DateFormat.ISODate)
            if parsed.isValid():
                self.history_end_input.setDateTime(parsed)

        self.auto_refresh_check.setChecked(_to_bool(self._settings.value("auto_refresh", False)))
        self.interval_seconds_input.setValue(int(self._settings.value("interval_seconds", self.interval_seconds_input.value(), int)))
        self.append_mode_check.setChecked(_to_bool(self._settings.value("append_mode", False)))
        self.nationwide_fallback_check.setChecked(_to_bool(self._settings.value("nationwide_fallback", False)))
        self.noaa_token_input.setText(self._settings.value("noaa_cdo_token", "", str))
        self.nearby_max_pages_input.setValue(
            int(self._settings.value("nearby_max_pages", self.nearby_max_pages_input.value(), int))
        )
        vector_mode = self._settings.value("wind_vector_mode", self.wind_vector_mode_combo.currentText(), str)
        if self.wind_vector_mode_combo.findText(vector_mode) >= 0:
            self.wind_vector_mode_combo.setCurrentText(vector_mode)
        self.prevailing_use_marker_check.setChecked(_to_bool(self._settings.value("prevailing_use_marker", False)))
        self.prevailing_place_marker_check.setChecked(
            _to_bool(self._settings.value("prevailing_marker_placement", False))
        )
        self.prevailing_buffer_ring_input.setValue(float(self._settings.value("prevailing_buffer_ring", 10.0, float)))
        self.prevailing_weight_curve_input.setValue(float(self._settings.value("prevailing_weight_curve", 1.0, float)))
        marker_lat = self._settings.value("prevailing_marker_lat", None)
        marker_lon = self._settings.value("prevailing_marker_lon", None)
        if marker_lat is not None and marker_lon is not None:
            try:
                self._prevailing_marker = (float(marker_lat), float(marker_lon))
            except (TypeError, ValueError):
                self._prevailing_marker = None
        self.prevailing_show_arrow_check.setChecked(
            _to_bool(self._settings.value("prevailing_show_arrow", self.prevailing_show_arrow_check.isChecked()))
        )
        self.prevailing_show_text_check.setChecked(
            _to_bool(self._settings.value("prevailing_show_text", self.prevailing_show_text_check.isChecked()))
        )
        self._update_prevailing_unit_labels()
        self._update_noaa_token_status()
        self._push_prevailing_marker()
        self._push_prevailing_overlay()

    def _update_noaa_token_status(self) -> None:
        configured = bool(self.noaa_token_input.text().strip())
        if configured:
            self.noaa_token_status_label.setText("Configured")
            self.noaa_token_status_label.setStyleSheet("color: green;")
        else:
            self.noaa_token_status_label.setText(
                "Not configured — CDO features unavailable (NWS live/historical unaffected)"
            )
            self.noaa_token_status_label.setStyleSheet("color: orange;")

    @Slot()
    def _copy_logs(self) -> None:
        QApplication.clipboard().setText(self.log_output.toPlainText())
        self._append_log("Logs copied to clipboard.")

    @Slot()
    def _save_logs(self) -> None:
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Save Logs",
            "wind_fetch_logs.txt",
            "Text Files (*.txt)",
        )
        if not out:
            return
        try:
            Path(out).write_text(self.log_output.toPlainText(), encoding="utf-8")
            self._append_log(f"Logs saved: {out}")
        except Exception as exc:
            self.status_label.setText("Log save failed.")
            QMessageBox.critical(self, "Save Error", f"Could not save logs: {exc}")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.append(f"[{timestamp}] {message}")
        if self.log_output.document().blockCount() > MAX_LOG_LINES:
            cursor = self.log_output.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            blocks_to_remove = self.log_output.document().blockCount() - MAX_LOG_LINES
            for _ in range(blocks_to_remove):
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)

    @Slot(int, str)
    def _on_fetch_progress(self, value: int, stage: str) -> None:
        self._set_progress(value, stage)

    def _set_progress(self, value: int, stage: str) -> None:
        clamped = max(0, min(100, int(value)))
        self.fetch_progress_bar.setValue(clamped)
        self.fetch_progress_bar.setFormat(f"{stage}: {clamped}%")

    @Slot()
    def _update_countdown(self) -> None:
        if not self.auto_refresh_check.isChecked() or self._next_refresh_at is None:
            self.countdown_label.setText("Next refresh in: -")
            return
        remaining = int((self._next_refresh_at - datetime.now(timezone.utc)).total_seconds())
        if remaining < 0:
            remaining = 0
        self.countdown_label.setText(f"Next refresh in: {remaining}s")

    def _schedule_next_refresh(self) -> None:
        seconds = self.interval_seconds_input.value()
        self.refresh_timer.start(seconds * 1000)
        self._next_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        self._update_countdown()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._is_closing = True
        if self._active_fetch_thread is not None and self._active_fetch_thread.isRunning():
            self._append_log("Close requested: cancelling active fetch thread...")
            self._active_fetch_thread.cancel()
            if not self._active_fetch_thread.wait(5000):
                self._is_closing = False
                QMessageBox.information(
                    self,
                    "Still Shutting Down",
                    "A fetch is still being cancelled. Please wait a few seconds and close again.",
                )
                event.ignore()
                return
        self._save_gui_settings()
        super().closeEvent(event)

    def _apply_table_filters(self, *_: object) -> None:
        text_filter = self.table_filter_input.text().strip().lower()
        source_filter = self.source_filter_combo.currentText()
        with_speed_only = self.with_speed_only_check.isChecked()
        with_gust_only = self.with_gust_only_check.isChecked()
        with_direction_only = self.with_direction_only_check.isChecked()
        hide_stale = self.hide_stale_timestamp_check.isChecked()
        stale_threshold_seconds = self.stale_minutes_input.value() * 60
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        filtered_payload = []
        visible_count = 0
        stats_rows: list[dict] = []

        for row in range(self.table.rowCount()):
            id_item = self.table.item(row, 0)
            if id_item is None:
                self.table.setRowHidden(row, True)
                continue

            meta = id_item.data(STATION_META_ROLE)
            if not isinstance(meta, dict):
                self.table.setRowHidden(row, True)
                continue

            station_id_lc = str(meta.get("station_id", "")).lower()
            station_name_lc = str(meta.get("station_name_lc", "")).lower()
            matches_text = True
            if text_filter:
                matches_text = text_filter in station_id_lc or text_filter in station_name_lc

            source = str(meta.get("source", ""))
            matches_source = source_filter == "All Sources" or source == source_filter
            matches_speed = (meta.get("wind_speed_mps") is not None) if with_speed_only else True
            matches_gust = (meta.get("wind_gust_mps") is not None) if with_gust_only else True
            matches_direction = (meta.get("wind_direction_deg") is not None) if with_direction_only else True

            ts_epoch = int(meta.get("timestamp_epoch", 0) or 0)
            is_stale = True
            if ts_epoch > 0:
                age_seconds = max(0, now_epoch - ts_epoch)
                is_stale = age_seconds >= stale_threshold_seconds
            matches_stale = (not is_stale) if hide_stale else True

            visible = (
                matches_text
                and matches_source
                and matches_speed
                and matches_gust
                and matches_direction
                and matches_stale
            )
            self.table.setRowHidden(row, not visible)
            if visible:
                visible_count += 1
                map_entry = meta.get("map_entry")
                if isinstance(map_entry, dict):
                    filtered_payload.append(map_entry)
                stats_rows.append(meta)

        self.status_label.setText(f"Showing {visible_count}/{len(self._latest_results)} stations after filters.")
        self.map_view.page().runJavaScript(
            f"if (typeof window.setStations === 'function') {{ window.setStations({json.dumps(filtered_payload)}); }}"
        )
        self._current_stats = self._compute_statistics(stats_rows)
        self._render_statistics()
        self._current_prevailing = self._compute_prevailing_wind(stats_rows)
        self._update_prevailing_panel(self._current_prevailing, visible_count)
        self._push_prevailing_overlay()

    def _compute_prevailing_wind(self, rows: list[dict]) -> dict | None:
        valid = [
            row
            for row in rows
            if row.get("wind_speed_mps") is not None and row.get("wind_direction_deg") is not None
        ]
        if not valid:
            return None

        weighted = self.prevailing_use_marker_check.isChecked() and self._prevailing_marker is not None
        if weighted:
            self._validate_prevailing_marker_within_bbox()
            weighted = self._prevailing_marker is not None

        u_sum = 0.0
        v_sum = 0.0
        speed_sum = 0.0
        weight_sum = 0.0
        buffer_km = distance_to_km(self.prevailing_buffer_ring_input.value(), self.dimension_unit_combo.currentText())
        curve_p = float(self.prevailing_weight_curve_input.value())
        effective_ring_km = max(buffer_km, 0.001)
        marker_lat = self._prevailing_marker[0] if self._prevailing_marker is not None else None
        marker_lon = self._prevailing_marker[1] if self._prevailing_marker is not None else None
        for row in valid:
            speed_mps = float(row.get("wind_speed_mps", 0.0) or 0.0)
            direction_deg = float(row.get("wind_direction_deg", 0.0) or 0.0)
            rad = math.radians(direction_deg)
            weight = 1.0
            if weighted and marker_lat is not None and marker_lon is not None:
                lat = float(row.get("latitude", 0.0) or 0.0)
                lon = float(row.get("longitude", 0.0) or 0.0)
                distance_km = _haversine_km(marker_lat, marker_lon, lat, lon)
                weight = 1.0 / ((1.0 + (distance_km / effective_ring_km)) ** curve_p)
            u_sum += weight * (-speed_mps * math.sin(rad))
            v_sum += weight * (-speed_mps * math.cos(rad))
            speed_sum += weight * speed_mps
            weight_sum += weight

        n = len(valid)
        if weight_sum <= 0:
            return None
        u_mean = u_sum / weight_sum
        v_mean = v_sum / weight_sum
        resultant_speed_mps = math.sqrt(u_mean * u_mean + v_mean * v_mean)
        base_from_deg = (math.degrees(math.atan2(-u_mean, -v_mean)) + 360.0) % 360.0

        direction_mode = "downwind" if self.wind_vector_mode_combo.currentText().startswith("Downwind") else "upwind"
        display_direction_deg = (base_from_deg + 180.0) % 360.0 if direction_mode == "downwind" else base_from_deg
        speed_unit = self.speed_unit_combo.currentText()
        speed_display = _convert_speed_from_mps(resultant_speed_mps, speed_unit)
        mean_speed_mps = speed_sum / weight_sum if weight_sum > 0 else 0.0
        consistency = (resultant_speed_mps / mean_speed_mps) if mean_speed_mps > 0 else 0.0

        return {
            "speed_mps": resultant_speed_mps,
            "speed_display": speed_display,
            "speed_unit": speed_unit,
            "direction_deg": display_direction_deg,
            "compass": _deg_to_compass(display_direction_deg),
            "consistency": consistency,
            "used_count": n,
            "mode": "Weighted by marker distance" if weighted else "Uniform",
            "marker": (self._prevailing_marker[0], self._prevailing_marker[1]) if self._prevailing_marker else None,
            "buffer_km": buffer_km,
            "curve_p": curve_p,
        }

    def _update_prevailing_panel(self, prevailing: dict | None, visible_count: int) -> None:
        if not prevailing:
            self.prevailing_speed_label.setText("n/a")
            self.prevailing_direction_label.setText("n/a")
            self.prevailing_compass_label.setText("n/a")
            self.prevailing_used_label.setText(f"0 / {visible_count}")
            self.prevailing_confidence_label.setText("n/a")
            self.prevailing_confidence_level_label.setText("n/a")
            self.prevailing_weight_mode_label.setText(
                "Weighted by marker distance" if self.prevailing_use_marker_check.isChecked() and self._prevailing_marker else "Uniform"
            )
            if self._prevailing_marker is not None:
                self.prevailing_marker_status_label.setText(
                    f"{self._prevailing_marker[0]:.4f}, {self._prevailing_marker[1]:.4f}"
                )
            else:
                self.prevailing_marker_status_label.setText("No marker placed")
            return

        self.prevailing_speed_label.setText(
            f"{float(prevailing.get('speed_display', 0.0)):.2f} {prevailing.get('speed_unit', '')}"
        )
        self.prevailing_direction_label.setText(f"{float(prevailing.get('direction_deg', 0.0)):.1f} deg")
        self.prevailing_compass_label.setText(str(prevailing.get("compass", "n/a")))
        self.prevailing_used_label.setText(f"{int(prevailing.get('used_count', 0))} / {visible_count}")
        consistency = float(prevailing.get("consistency", 0.0))
        self.prevailing_confidence_label.setText(f"{consistency:.2f}")
        self.prevailing_confidence_level_label.setText(_consistency_level(consistency))
        mode_text = str(prevailing.get("mode", "Uniform"))
        if mode_text != "Uniform":
            mode_text += f" (p={float(prevailing.get('curve_p', 1.0)):.1f})"
        self.prevailing_weight_mode_label.setText(mode_text)
        marker = prevailing.get("marker")
        if isinstance(marker, tuple) and len(marker) == 2:
            self.prevailing_marker_status_label.setText(f"{float(marker[0]):.4f}, {float(marker[1]):.4f}")
        else:
            self.prevailing_marker_status_label.setText("No marker placed")

    def _push_prevailing_overlay(self) -> None:
        bbox = self._last_bbox
        if bbox is None:
            bbox = bbox_from_centroid(
                self.lat_input.value(),
                self.lon_input.value(),
                self.width_input.value(),
                self.height_input.value(),
                self.dimension_unit_combo.currentText(),
            )

        prevailing = self._current_prevailing
        has_prevailing = prevailing is not None
        text = "Prevailing wind: n/a"
        direction_deg = None
        speed_mps = None
        if prevailing:
            direction_deg = float(prevailing.get("direction_deg", 0.0))
            speed_mps = float(prevailing.get("speed_mps", 0.0))
            text = (
                f"Prevailing: {direction_deg:.0f} deg {prevailing.get('compass', '')} @ "
                f"{float(prevailing.get('speed_display', 0.0)):.2f} {prevailing.get('speed_unit', '')}"
            )

        payload = {
            "has_prevailing": has_prevailing,
            "show_arrow": self.prevailing_show_arrow_check.isChecked(),
            "show_text": self.prevailing_show_text_check.isChecked(),
            "direction_deg": direction_deg,
            "speed_mps": speed_mps,
            "text": text,
            "centroid_lat": self.lat_input.value(),
            "centroid_lon": self.lon_input.value(),
            "min_lat": bbox[0],
            "min_lon": bbox[1],
            "max_lat": bbox[2],
            "max_lon": bbox[3],
        }
        self.map_view.page().runJavaScript(
            f"if (typeof window.setPrevailingOverlay === 'function') {{ window.setPrevailingOverlay({json.dumps(payload)}); }}"
        )

    def _set_table_item(self, row: int, col: int, value: str, sort_value: Any | None = None) -> QTableWidgetItem:
        item = SortableTableWidgetItem(value)
        if sort_value is not None:
            item.setData(Qt.ItemDataRole.UserRole, sort_value)
        self.table.setItem(row, col, item)
        return item

    @Slot()
    def _update_lapsed_column(self) -> None:
        if self.table.rowCount() == 0:
            return
        for row in range(self.table.rowCount()):
            id_item = self.table.item(row, 0)
            if id_item is None:
                continue
            meta = id_item.data(STATION_META_ROLE)
            if not isinstance(meta, dict):
                continue
            ts_epoch = int(meta.get("timestamp_epoch", 0) or 0)
            lapsed_text = _format_lapsed_since_epoch(ts_epoch)
            lapsed_seconds = _lapsed_seconds_epoch(ts_epoch)
            item = self.table.item(row, 8)
            if item is None:
                self._set_table_item(row, 8, lapsed_text, sort_value=lapsed_seconds)
            else:
                item.setText(lapsed_text)
                item.setData(Qt.ItemDataRole.UserRole, lapsed_seconds)
            map_entry = meta.get("map_entry")
            if isinstance(map_entry, dict):
                map_entry["lapsed_since_update"] = lapsed_text
                meta["map_entry"] = map_entry
            meta["lapsed_seconds"] = lapsed_seconds
            id_item.setData(STATION_META_ROLE, meta)

    @Slot()
    def _recompute_statistics(self) -> None:
        self._apply_table_filters()

    @Slot()
    def _open_statistics_dashboard(self) -> None:
        if self._stats_dashboard is None:
            self._stats_dashboard = StatisticsDashboardDialog(self)
            self._stats_dashboard.refresh_requested.connect(self._recompute_statistics)
        self._recompute_statistics()
        self._push_statistics_to_dashboard(force=True)
        self._stats_dashboard.show()
        self._stats_dashboard.raise_()
        self._stats_dashboard.activateWindow()

    @Slot()
    def _copy_statistics_summary(self) -> None:
        summary = self.stats_output.toPlainText().strip()
        if not summary:
            self._recompute_statistics()
            summary = self.stats_output.toPlainText().strip()
        if not summary:
            QMessageBox.information(self, "No Statistics", "No statistics are available to copy.")
            return
        QApplication.clipboard().setText(summary)
        self._append_log("Statistics copied to clipboard.")

    @Slot()
    def _export_statistics_json(self) -> None:
        if not self._current_stats:
            self._recompute_statistics()
        if not self._current_stats:
            QMessageBox.information(self, "No Statistics", "No statistics are available to export.")
            return
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Export Statistics JSON",
            "wind_statistics.json",
            "JSON (*.json)",
        )
        if not out:
            return
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "speed_unit": self.speed_unit_combo.currentText(),
            "stale_threshold_minutes": self.stale_minutes_input.value(),
            "current_visible_statistics": self._current_stats,
            "previous_fetch_statistics": self._last_fetch_stats,
            "prior_fetch_statistics": self._previous_fetch_stats,
        }
        try:
            Path(out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._append_log(f"Statistics exported: {out}")
            self.status_label.setText(f"Statistics exported: {out}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", f"Statistics export failed: {exc}")

    def _compute_statistics(self, rows: list[dict]) -> dict:
        histogram_bins = self._stats_dashboard.histogram_bins() if self._stats_dashboard is not None else 8
        total = len(rows)
        speed_values = [float(row["speed_display"]) for row in rows if row.get("speed_display") is not None]
        gust_values = [float(row["gust_display"]) for row in rows if row.get("gust_display") is not None]
        direction_values = [float(row["wind_direction_deg"]) for row in rows if row.get("wind_direction_deg") is not None]
        lapsed_values = [int(row.get("lapsed_seconds", 0)) for row in rows]
        stale_threshold_seconds = self.stale_minutes_input.value() * 60
        stale_count = sum(1 for value in lapsed_values if value >= stale_threshold_seconds)
        freshness_buckets = _freshness_buckets(lapsed_values)

        direction_hist = {label: 0 for label in ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]}
        for deg in direction_values:
            direction_hist[_direction_sector(deg)] += 1

        stale_rows = [row for row in rows if int(row.get("lapsed_seconds", 0)) >= stale_threshold_seconds]
        freshest_row = min(rows, key=lambda row: int(row.get("lapsed_seconds", 0)), default=None)
        stalest_row = max(rows, key=lambda row: int(row.get("lapsed_seconds", 0)), default=None)

        return {
            "counts": {
                "total": total,
                "with_speed": len(speed_values),
                "with_gust": len(gust_values),
                "with_direction": len(direction_values),
                "missing_speed": total - len(speed_values),
                "missing_gust": total - len(gust_values),
                "missing_direction": total - len(direction_values),
                "stale_count": stale_count,
            },
            "speed": _metric_stats(speed_values),
            "gust": _metric_stats(gust_values),
            "direction": {
                "circular_mean_deg": _circular_mean_deg(direction_values),
                "sector_distribution": direction_hist,
            },
            "freshness": {
                "min_lapsed_seconds": min(lapsed_values) if lapsed_values else None,
                "max_lapsed_seconds": max(lapsed_values) if lapsed_values else None,
                "mean_lapsed_seconds": round(statistics.mean(lapsed_values), 2) if lapsed_values else None,
                "freshest_station_id": freshest_row.get("station_id") if freshest_row else None,
                "stalest_station_id": stalest_row.get("station_id") if stalest_row else None,
                "stale_station_ids": [str(row.get("station_id", "")) for row in stale_rows],
                "bucket_distribution": freshness_buckets,
            },
            "histograms": {
                "speed": _histogram(speed_values, bins=histogram_bins),
                "gust": _histogram(gust_values, bins=histogram_bins),
            },
            "raw": {
                "speed_values": speed_values,
                "gust_values": gust_values,
            },
        }

    def _render_statistics(self) -> None:
        if not self._current_stats:
            self.stats_output.setPlainText("No statistics available yet.")
            self._push_statistics_to_dashboard()
            return

        counts = self._current_stats.get("counts", {})
        speed_stats = self._current_stats.get("speed", {})
        gust_stats = self._current_stats.get("gust", {})
        direction_stats = self._current_stats.get("direction", {})
        freshness_stats = self._current_stats.get("freshness", {})
        histograms = self._current_stats.get("histograms", {})

        lines = [
            "Computed Statistics",
            "",
            f"Visible stations: {counts.get('total', 0)}",
            (
                f"With speed/gust/direction: {counts.get('with_speed', 0)} / "
                f"{counts.get('with_gust', 0)} / {counts.get('with_direction', 0)}"
            ),
            (
                f"Missing speed/gust/direction: {counts.get('missing_speed', 0)} / "
                f"{counts.get('missing_gust', 0)} / {counts.get('missing_direction', 0)}"
            ),
            f"Stale count (>{self.stale_minutes_input.value()}m): {counts.get('stale_count', 0)}",
            "",
            f"Speed ({self.speed_unit_combo.currentText()}): {_stats_line(speed_stats)}",
            f"Gust ({self.speed_unit_combo.currentText()}): {_stats_line(gust_stats)}",
            (
                "Direction: "
                f"circular_mean={_fmt_optional(direction_stats.get('circular_mean_deg'), 2)} deg"
            ),
            (
                "Freshness (lapsed): "
                f"min={_format_lapsed_since_epoch(freshness_stats.get('min_lapsed_seconds'))}, "
                f"mean={_format_lapsed_since_epoch(_safe_int(freshness_stats.get('mean_lapsed_seconds')))}, "
                f"max={_format_lapsed_since_epoch(freshness_stats.get('max_lapsed_seconds'))}"
            ),
            (
                f"Freshest/Stalest station: {freshness_stats.get('freshest_station_id', 'n/a')} / "
                f"{freshness_stats.get('stalest_station_id', 'n/a')}"
            ),
            "",
            "Speed Histogram:",
        ]
        lines.extend(_histogram_lines(histograms.get("speed", [])))
        lines.append("")
        lines.append("Gust Histogram:")
        lines.extend(_histogram_lines(histograms.get("gust", [])))
        lines.append("")
        lines.append("Direction Distribution:")
        sector_distribution = direction_stats.get("sector_distribution", {})
        for sector in ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]:
            lines.append(f"  {sector}: {sector_distribution.get(sector, 0)}")
        lines.append("")
        lines.append("Freshness Buckets:")
        lines.extend(_histogram_lines(freshness_stats.get("bucket_distribution", [])))

        delta_lines = self._delta_statistics_lines()
        if delta_lines:
            lines.append("")
            lines.append("Delta vs Previous Fetch:")
            lines.extend(delta_lines)

        self.stats_output.setPlainText("\n".join(lines))
        self._push_statistics_to_dashboard()

    def _push_statistics_to_dashboard(self, force: bool = False) -> None:
        if self._stats_dashboard is None or not self._current_stats:
            return
        if not force and (not self._stats_dashboard.isVisible() or not self._stats_dashboard.auto_refresh_enabled()):
            return
        self._stats_dashboard.set_statistics(
            self._current_stats,
            self._previous_fetch_stats,
            self.speed_unit_combo.currentText(),
            self.stale_minutes_input.value(),
            self._delta_statistics_lines(),
        )

    def _delta_statistics_lines(self) -> list[str]:
        if not self._previous_fetch_stats:
            return ["  n/a (need at least two fetches)"]
        previous_counts = self._previous_fetch_stats.get("counts", {})
        current_counts = self._current_stats.get("counts", {})
        previous_speed = self._previous_fetch_stats.get("speed", {})
        current_speed = self._current_stats.get("speed", {})
        previous_gust = self._previous_fetch_stats.get("gust", {})
        current_gust = self._current_stats.get("gust", {})

        return [
            f"  Visible stations: {_delta_line(current_counts.get('total'), previous_counts.get('total'))}",
            f"  With speed: {_delta_line(current_counts.get('with_speed'), previous_counts.get('with_speed'))}",
            f"  With gust: {_delta_line(current_counts.get('with_gust'), previous_counts.get('with_gust'))}",
            f"  Mean speed: {_delta_line(current_speed.get('mean'), previous_speed.get('mean'))}",
            f"  Mean gust: {_delta_line(current_gust.get('mean'), previous_gust.get('mean'))}",
        ]

    def _history_start_iso(self) -> str:
        dt = self.history_start_input.dateTime().toPython()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    def _history_end_iso(self) -> str:
        dt = self.history_end_input.dateTime().toPython()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    def _export_metadata(self) -> dict:
        meta = {
            "mode": self.mode_combo.currentText(),
            "centroid": {
                "lat": self.lat_input.value(),
                "lon": self.lon_input.value(),
            },
            "dimensions": {
                "width": self.width_input.value(),
                "height": self.height_input.value(),
                "unit": self.dimension_unit_combo.currentText(),
            },
            "speed_unit": self.speed_unit_combo.currentText(),
            "noaa_cdo_token_configured": bool(self.noaa_token_input.text().strip()),
            "history_start": self._history_start_iso(),
            "history_end": self._history_end_iso(),
            "bbox": self._last_bbox,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        meta.update(self._last_query_metadata)
        return meta

    def _merge_observation_snapshots(
        self,
        existing: List[WindStationObservation],
        incoming: List[WindStationObservation],
    ) -> List[WindStationObservation]:
        dedup: Dict[tuple[str, str], WindStationObservation] = {}
        for obs in existing + incoming:
            ts = obs.timestamp.isoformat() if obs.timestamp else ""
            dedup[(obs.station_id, ts)] = obs
        merged = list(dedup.values())
        merged.sort(key=lambda item: (item.station_id, _normalize_ts(item.timestamp)))
        return merged

    def _merge_historical_series(
        self,
        existing: Dict[str, List[WindStationObservation]],
        incoming: Dict[str, List[WindStationObservation]],
    ) -> Dict[str, List[WindStationObservation]]:
        output: Dict[str, List[WindStationObservation]] = {}
        station_ids = set(existing.keys()) | set(incoming.keys())
        for station_id in station_ids:
            combined = existing.get(station_id, []) + incoming.get(station_id, [])
            dedup: Dict[str, WindStationObservation] = {}
            for obs in combined:
                ts = obs.timestamp.isoformat() if obs.timestamp else f"{obs.station_id}-none"
                dedup[ts] = obs
            values = list(dedup.values())
            values.sort(key=lambda item: _normalize_ts(item.timestamp))
            output[station_id] = values
        return output


def _deserialize_observations(items: object) -> List[WindStationObservation]:
    if not isinstance(items, list):
        return []
    output: List[WindStationObservation] = []
    for entry in items:
        if isinstance(entry, dict):
            output.append(WindStationObservation.from_dict(entry))
    return output


def _deserialize_series(data: object) -> Dict[str, List[WindStationObservation]]:
    if not isinstance(data, dict):
        return {}
    output: Dict[str, List[WindStationObservation]] = {}
    for station_id, values in data.items():
        if not isinstance(values, list):
            continue
        output[str(station_id)] = [
            WindStationObservation.from_dict(item)
            for item in values
            if isinstance(item, dict)
        ]
    return output


def _fmt_numeric(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_ts(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_lapsed_since(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    ts = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    delta_seconds = int((datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())
    if delta_seconds < 0:
        delta_seconds = 0
    hours, rem = divmod(delta_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_lapsed_since_epoch(epoch_seconds: int | float | None) -> str:
    if epoch_seconds is None:
        return "n/a"
    try:
        value = int(epoch_seconds)
    except (TypeError, ValueError):
        return "n/a"
    if value <= 0:
        return "n/a"
    delta_seconds = int(datetime.now(timezone.utc).timestamp()) - value
    if delta_seconds < 0:
        delta_seconds = 0
    hours, rem = divmod(delta_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_lapsed_seconds_value(seconds_value: object) -> str:
    if seconds_value is None:
        return "n/a"
    try:
        total_seconds = int(float(seconds_value))
    except (TypeError, ValueError):
        return "n/a"
    if total_seconds < 0:
        total_seconds = 0
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _lapsed_seconds(value: datetime | None) -> int:
    if value is None:
        return 2_147_483_647
    ts = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    delta_seconds = int((datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())
    if delta_seconds < 0:
        return 0
    return delta_seconds


def _lapsed_seconds_epoch(epoch_seconds: int | float | None) -> int:
    if epoch_seconds is None:
        return 2_147_483_647
    try:
        value = int(epoch_seconds)
    except (TypeError, ValueError):
        return 2_147_483_647
    if value <= 0:
        return 2_147_483_647
    delta_seconds = int(datetime.now(timezone.utc).timestamp()) - value
    if delta_seconds < 0:
        return 0
    return delta_seconds


def _timestamp_epoch_seconds(value: datetime | None) -> int:
    if value is None:
        return 0
    ts = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(ts.astimezone(timezone.utc).timestamp())


def _sort_numeric_or_none(value: float | None) -> float:
    if value is None:
        return float("inf")
    return value


def _convert_speed_from_mps(speed_mps: float, speed_unit: str) -> float:
    if speed_unit == "mph":
        return speed_mps * 2.2369362920544
    if speed_unit == "kt":
        return speed_mps * 1.9438444924406
    return speed_mps


def _deg_to_compass(direction_deg: float) -> str:
    sectors = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int((direction_deg % 360.0 + 22.5) // 45.0) % 8
    return sectors[idx]


def _consistency_level(consistency: float) -> str:
    if consistency < 0.40:
        return "Low"
    if consistency <= 0.70:
        return "Moderate"
    return "High"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
    return 2.0 * radius_km * math.asin(min(1.0, math.sqrt(a)))


def _metric_stats(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "std_dev": None,
            "p25": None,
            "p75": None,
            "p90": None,
        }
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "std_dev": statistics.pstdev(values),
        "p25": _percentile(ordered, 25),
        "p75": _percentile(ordered, 75),
        "p90": _percentile(ordered, 90),
    }


def _percentile(sorted_values: list[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (percentile / 100.0)
    low_index = int(math.floor(rank))
    high_index = int(math.ceil(rank))
    if low_index == high_index:
        return sorted_values[low_index]
    low_value = sorted_values[low_index]
    high_value = sorted_values[high_index]
    weight = rank - low_index
    return low_value + (high_value - low_value) * weight


def _direction_sector(degrees: float) -> str:
    normalized = degrees % 360.0
    sectors = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    index = int((normalized + 22.5) // 45.0) % 8
    return sectors[index]


def _circular_mean_deg(values: list[float]) -> float | None:
    if not values:
        return None
    sin_sum = sum(math.sin(math.radians(value)) for value in values)
    cos_sum = sum(math.cos(math.radians(value)) for value in values)
    if sin_sum == 0 and cos_sum == 0:
        return None
    angle = math.degrees(math.atan2(sin_sum, cos_sum))
    return (angle + 360.0) % 360.0


def _histogram(values: list[float], bins: int = 5) -> list[dict]:
    if not values:
        return []
    if bins < 1:
        bins = 1
    minimum = min(values)
    maximum = max(values)
    if math.isclose(minimum, maximum):
        return [{"label": f"{minimum:.2f}", "count": len(values)}]

    width = (maximum - minimum) / bins
    counts = [0 for _ in range(bins)]
    for value in values:
        index = int((value - minimum) / width)
        if index >= bins:
            index = bins - 1
        counts[index] += 1

    output: list[dict] = []
    for idx, count in enumerate(counts):
        low = minimum + idx * width
        high = minimum + (idx + 1) * width
        output.append({"label": f"{low:.2f} - {high:.2f}", "count": count})
    return output


def _freshness_buckets(lapsed_seconds: list[int]) -> list[dict]:
    labels = ["<15m", "15-60m", "1-3h", "3-6h", "6h+"]
    counts = [0, 0, 0, 0, 0]
    for value in lapsed_seconds:
        if value < 15 * 60:
            counts[0] += 1
        elif value < 60 * 60:
            counts[1] += 1
        elif value < 3 * 60 * 60:
            counts[2] += 1
        elif value < 6 * 60 * 60:
            counts[3] += 1
        else:
            counts[4] += 1
    return [{"label": labels[idx], "count": counts[idx]} for idx in range(len(labels))]


def _histogram_lines(buckets: list[dict]) -> list[str]:
    if not buckets:
        return ["  n/a"]
    max_count = max(int(bucket.get("count", 0)) for bucket in buckets)
    scale = 20 / max_count if max_count > 0 else 1
    lines: list[str] = []
    for bucket in buckets:
        label = str(bucket.get("label", ""))
        count = int(bucket.get("count", 0))
        bar = "#" * int(round(count * scale))
        lines.append(f"  {label}: {bar} ({count})")
    return lines


def _stats_line(stats_obj: dict) -> str:
    if not stats_obj or stats_obj.get("count", 0) == 0:
        return "n/a"
    return (
        f"min={_fmt_optional(stats_obj.get('min'), 2)}, "
        f"max={_fmt_optional(stats_obj.get('max'), 2)}, "
        f"mean={_fmt_optional(stats_obj.get('mean'), 2)}, "
        f"median={_fmt_optional(stats_obj.get('median'), 2)}, "
        f"std={_fmt_optional(stats_obj.get('std_dev'), 2)}, "
        f"p25={_fmt_optional(stats_obj.get('p25'), 2)}, "
        f"p75={_fmt_optional(stats_obj.get('p75'), 2)}, "
        f"p90={_fmt_optional(stats_obj.get('p90'), 2)}"
    )


def _fmt_optional(value: object, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _delta_line(current: object, previous: object) -> str:
    current_num = _safe_float(current)
    previous_num = _safe_float(previous)
    if current_num is None or previous_num is None:
        return "n/a"
    delta = current_num - previous_num
    return f"{current_num:.2f} (delta {delta:+.2f})"


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
