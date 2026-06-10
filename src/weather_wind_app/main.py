"""Application entrypoint for the wind sensor explorer."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyleFactory

from weather_wind_app.ui.main_window import MainWindow


def configure_style(app: QApplication) -> None:
    preferred = ["WindowsVista", "Windows", "Fusion"]
    available = set(QStyleFactory.keys())
    for style in preferred:
        if style in available:
            app.setStyle(style)
            break


def main() -> int:
    app = QApplication(sys.argv)
    icon_path = Path(__file__).resolve().parent / "assets" / "app_icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    configure_style(app)

    window = MainWindow()
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
