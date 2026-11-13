"""Shared constants, pure utilities, and SVG icon helpers used across all modules."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


@dataclass(frozen=True)
class AppPaths:
    resources_dir: Path
    data_dir: Path
    reports_dir: Path
    logs_dir: Path
    backups_dir: Path


def resolve_app_paths(data_dir_override: Path | str | None = None) -> AppPaths:
    """Resolve read-only resource and writable-data directories."""
    if getattr(sys, "frozen", False):
        resources_dir = Path(sys._MEIPASS) / "resources"  # type: ignore[attr-defined]
        default_data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DeepVac" / "data"
    else:
        project_root = Path(__file__).resolve().parent.parent
        resources_dir = project_root / "resources"
        default_data_dir = project_root / "data"

    if data_dir_override is not None:
        data_dir = Path(data_dir_override)
    elif os.environ.get("DEEPVAC_DATA_DIR"):
        data_dir = Path(os.environ["DEEPVAC_DATA_DIR"])
    else:
        data_dir = default_data_dir

    return AppPaths(
        resources_dir=resources_dir,
        data_dir=data_dir,
        reports_dir=data_dir / "reports",
        logs_dir=data_dir / "logs",
        backups_dir=data_dir / "backups",
    )


_paths = resolve_app_paths()
RESOURCES_DIR = _paths.resources_dir
DATA_DIR = _paths.data_dir
REPORTS_DIR = _paths.reports_dir

LOGO_PATH = str(RESOURCES_DIR / "logo.png")
LOGO_WHITE_PATH = str(RESOURCES_DIR / "logo-white.png")
ICON_PATH = str(RESOURCES_DIR / "icon.png")

COLORS = [
    "#8bd66f",
    "#4f7cff",
    "#51d6c7",
    "#f2bd52",
    "#ff6f7d",
    "#b792ff",
    "#f48fb1",
    "#7ec8ff",
]

RULE_COLOR_OPTIONS = [
    ("Blue", "#60a5fa"),
    ("Green", "#8bd66f"),
    ("Amber", "#f2bd52"),
    ("Red", "#ff6f7d"),
    ("Purple", "#b792ff"),
    ("Teal", "#51d6c7"),
]

_TAB_MIME = "application/deepvac-tab"
_gid = 0

PAGE_ANALYSIS = 0
PAGE_REPORTS = 1
PAGE_CONTROLLER = 2
PAGE_OPC = 3
PAGE_CAMPAIGNS = 4
PAGE_AI_AGENTS = 5
PAGE_COUNT = 6


def _new_gid() -> int:
    global _gid
    _gid += 1
    return _gid


def fmt(value, digits: int = 3) -> str:
    if value in (None, ""):
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 1000:
        return f"{number:,.1f}"
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def csv_escape(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if any(ch in text for ch in [",", '"', "\n", "\r"]):
        return '"' + text.replace('"', '""') + '"'
    return text


def _render_svg(name: str, color: str, size: int = 20) -> QPixmap:
    path = RESOURCES_DIR / "icons" / f"{name}.svg"
    try:
        svg_bytes = path.read_text(encoding="utf-8").replace("currentColor", color).encode()
        renderer = QSvgRenderer(QByteArray(svg_bytes))
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return pixmap
    except Exception:
        return QPixmap()


def _svg_icon(name: str, color: str = "#94a3b8", size: int = 20) -> QIcon:
    px = _render_svg(name, color, size)
    return QIcon(px) if not px.isNull() else QIcon()


def _nav_icon(name: str, muted: str, accent: str, size: int = 22) -> QIcon:
    icon = QIcon()
    icon.addPixmap(_render_svg(name, muted, size), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_render_svg(name, accent, size), QIcon.Mode.Normal, QIcon.State.On)
    return icon
