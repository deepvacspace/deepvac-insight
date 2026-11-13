"""Exports of a recorded run."""

import base64
import contextlib
import csv
import json
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMarginsF, QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPainterPath,
    QPdfWriter,
    QPen,
    QTextDocument,
)

from app.core.analysis import anomalies as anomaly_mod
from app.core.analysis.comparison import compare_configs, compare_kpis, match_steps

IMAGE_FORMATS = {"png": "PNG", "bmp": "BMP", "jpg": "JPEG", "jpeg": "JPEG"}
DATA_FORMATS = ("csv", "txt")
REPORT_FORMATS = ("html", "pdf")
CHART_WIDTH, CHART_HEIGHT = 1600, 700
MAX_EVENTS = 300

STAGE_COLORS = {"ramp": "#d97706", "setpoint": "#2563eb", "hold": "#16a34a"}
STAGE_NAMES = {"ramp": "Ramp", "setpoint": "Setpoint", "hold": "Hold"}
SEVERITY_COLORS = {"info": "#64748b", "warning": "#d97706", "critical": "#dc2626"}
ANOMALY_NAMES = {
    "limit": "Limit",
    "spike": "Spike",
    "flatline": "Flat signal",
    "gap": "Missing data",
    "oscillation": "Oscillation",
    "not_reached": "Setpoint not reached",
    "saturation": "Saturation",
}
CHANNEL_COLORS = {
    "temp": "#2563eb",
    "temp_ref": "#f97316",
    "temp_u": "#16a34a",
    "temp_u_p": "#e11d48",
    "temp_u_i": "#9333ea",
    "temp_u_d": "#0d9488",
}
FALLBACK_COLORS = [
    "#2563eb",
    "#f97316",
    "#16a34a",
    "#e11d48",
    "#9333ea",
    "#0d9488",
    "#ca8a04",
    "#475569",
    "#db2777",
    "#0284c7",
]
COMPARE_COLORS = [
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#9333ea",
    "#d97706",
    "#0d9488",
    "#db2777",
    "#475569",
]
EVENT_NAMES = {
    "job_start": "Job start",
    "job_end": "Job end",
    "setpoint_change": "Setpoint change",
    "ramp_start": "Ramp start",
    "ramp_end": "Ramp end",
    "pid_gains_change": "PID gains",
    "pid_band_change": "PID band",
    "pid_table_written": "PID table written",
    "status_change": "Status",
    "program_interval": "Program interval",
    "program_cycle": "Program cycle",
    "alarm_raised": "Alarm",
    "alarm_cleared": "Alarm cleared",
    "alarm_received": "PLC alarm",
    "operator_action": "Operator action",
    "plc_command": "PLC command",
    "app_error": "Error",
    "user_change": "User",
}
JOB_KINDS = {
    "manual_setpoint": "manual setpoint",
    "program": "program",
    "test_profile": "test profile",
    "ai_advisor": "AI Advisor (online)",
    "offline_advisor": "AI Advisor (offline tuning)",
    "external": "started outside this application",
    "job": "job",
}
RAMP_BIT, AWAIT_BIT = 9, 8

_paint_lock = threading.Lock()


class ExportError(ValueError):
    pass


@dataclass
class RunExport:
    """Data needed to export a run."""

    title: str
    start_epoch: float
    times: list[float]
    channels: dict[str, list[float]]
    analysis: object = None
    manifest: dict | None = None
    labels: dict[str, str] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    chamber: str = ""
    annotations: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    summary: dict | None = None
    samples: list | None = None

    @property
    def duration_s(self) -> float:
        return self.times[-1] - self.times[0] if len(self.times) > 1 else 0.0

    def label(self, name: str) -> str:
        return self.labels.get(name) or name

    def epoch(self, elapsed: float) -> float:
        return self.start_epoch + elapsed if math.isfinite(self.start_epoch) else math.nan


def channel_color(name: str, index: int) -> str:
    return CHANNEL_COLORS.get(name, FALLBACK_COLORS[index % len(FALLBACK_COLORS)])


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")[:60] or "run"


def number(value, precision: int = 4) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{precision}g}"


def seconds(value) -> str:
    """Formats a duration as hh:mm:ss."""
    if value is None or not math.isfinite(value):
        return "—"
    total = int(round(max(0.0, value)))
    days, total = divmod(total, 86400)
    hms = f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{days}d {hms}" if days else hms


def percent(fraction) -> str:
    return "—" if fraction is None or not math.isfinite(fraction) else f"{100.0 * fraction:.1f} %"


def pid_shares(pid) -> str:
    if pid is None or not math.isfinite(pid.share_p):
        return "—"
    return f"{100 * pid.share_p:.0f} / {100 * pid.share_i:.0f} / {100 * pid.share_d:.0f} %"


def date_time(epoch) -> str:
    if epoch is None or not math.isfinite(epoch):
        return "—"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _esc(text) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _csv_number(value) -> str:
    return "" if value is None or math.isnan(value) else f"{value:.10g}"


@dataclass
class Series:
    label: str
    color: str
    x: list[float]
    y: list[float]
    width: float = 2.0


@dataclass
class Region:
    start: float
    end: float
    label: str
    color: str


@dataclass
class Marker:
    at: float
    label: str
    color: str


def _nice_step(span: float, target: int) -> float:
    raw = span / max(1, target)
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    power = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * power:
            return m * power
    return 10 * power


def _time_step(span: float, target: int) -> float:
    raw = span / max(1, target)
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600):
        if raw <= step:
            return step
    return _nice_step(span, target)


def render_chart(
    series: list[Series],
    regions: list[Region] = (),
    markers: list[Marker] = (),
    title: str = "",
    width: int = CHART_WIDTH,
    height: int = CHART_HEIGHT,
    x_from: float | None = None,
    x_to: float | None = None,
) -> QImage:
    """Renders a line chart over elapsed time."""
    if QGuiApplication.instance() is None:
        raise ExportError("Charts need a Qt GUI application (start the agent API through app.api).")
    xs = [x for s in series for x in s.x if math.isfinite(x)]
    if not xs:
        raise ExportError("Nothing to draw: the channels have no samples.")
    x0 = min(xs) if x_from is None else x_from
    x1 = max(xs) if x_to is None else x_to
    if x1 <= x0:
        x1 = x0 + 1.0
    ys = [
        y
        for s in series
        for x, y in zip(s.x, s.y, strict=False)
        if x0 <= x <= x1 and y is not None and math.isfinite(y)
    ]
    if not ys:
        raise ExportError("Nothing to draw in that time range.")
    y0, y1 = min(ys), max(ys)
    pad = (y1 - y0) * 0.06 or max(1.0, abs(y1) * 0.05)
    y0, y1 = y0 - pad, y1 + pad

    with _paint_lock:
        image = QImage(width, height, QImage.Format_ARGB32)
        image.fill(QColor("#ffffff"))
        p = QPainter(image)
        p.setRenderHint(QPainter.Antialiasing)
        font = QFont("Segoe UI", 1)
        font.setPixelSize(max(11, height // 50))
        p.setFont(font)
        metrics = p.fontMetrics()

        legend, row, row_width = [[]], 0, 0.0
        for s in series:
            text = s.label if len(s.label) <= 48 else s.label[:47] + "…"
            entry = 28 + metrics.horizontalAdvance(text) + 22
            if legend[row] and row_width + entry > width - 78 - 24:
                legend.append([])
                row, row_width = row + 1, 0.0
            legend[row].append((s.color, text))
            row_width += entry
        top = 14 + (metrics.height() + 8 if title else 0) + len(legend) * (metrics.height() + 4) + 8
        plot = QRectF(78, top, width - 78 - 24, height - top - metrics.height() - 22)

        def px(x):
            return plot.left() + (x - x0) / (x1 - x0) * plot.width()

        def py(y):
            return plot.bottom() - (y - y0) / (y1 - y0) * plot.height()

        if title:
            bold = QFont(font)
            bold.setBold(True)
            p.setFont(bold)
            p.setPen(QColor("#0f172a"))
            p.drawText(QPointF(plot.left(), 14 + metrics.ascent()), title)
            p.setFont(font)

        for r in regions:
            a, b = max(r.start, x0), min(r.end, x1)
            if b <= a:
                continue
            fill = QColor(r.color)
            fill.setAlpha(26)
            rect = QRectF(px(a), plot.top(), max(1.0, px(b) - px(a)), plot.height())
            p.fillRect(rect, fill)
            if rect.width() > metrics.horizontalAdvance(r.label) + 8:
                p.setPen(QColor(r.color))
                p.drawText(QPointF(rect.left() + 4, plot.top() + metrics.ascent() + 3), r.label)

        grid = QPen(QColor("#e2e8f0"))
        grid.setWidthF(1.0)
        p.setPen(grid)
        y_step = _nice_step(y1 - y0, 6)
        y_ticks = []
        v = math.ceil(y0 / y_step) * y_step
        while v <= y1:
            y_ticks.append(v)
            v += y_step
        x_step = _time_step(x1 - x0, 8)
        x_ticks = []
        v = math.ceil(x0 / x_step) * x_step
        while v <= x1:
            x_ticks.append(v)
            v += x_step
        for v in y_ticks:
            p.drawLine(QPointF(plot.left(), py(v)), QPointF(plot.right(), py(v)))
        for v in x_ticks:
            p.drawLine(QPointF(px(v), plot.top()), QPointF(px(v), plot.bottom()))
        p.setPen(QColor("#64748b"))
        decimals = max(0, -math.floor(math.log10(y_step))) if y_step < 1 else 0
        for v in y_ticks:
            text = f"{v:.{decimals}f}"
            p.drawText(
                QPointF(
                    plot.left() - 8 - metrics.horizontalAdvance(text),
                    py(v) + metrics.ascent() / 2 - 1,
                ),
                text,
            )
        for v in x_ticks:
            text = seconds(v)
            p.drawText(
                QPointF(
                    px(v) - metrics.horizontalAdvance(text) / 2,
                    plot.bottom() + metrics.ascent() + 6,
                ),
                text,
            )
        p.setPen(QPen(QColor("#cbd5e1")))
        p.drawRect(plot)

        for m in markers:
            if not x0 <= m.at <= x1:
                continue
            pen = QPen(QColor(m.color))
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(1.2)
            p.setPen(pen)
            p.drawLine(QPointF(px(m.at), plot.top()), QPointF(px(m.at), plot.bottom()))

        p.setClipRect(plot)
        for s in series:
            pen = QPen(QColor(s.color))
            pen.setWidthF(s.width)
            p.setPen(pen)
            path = QPainterPath()
            for piece in _pixel_columns(s, x0, x1, px):
                points = [
                    QPointF(c, py(y))
                    for c, lo, hi in piece
                    for y in ((lo, hi) if lo != hi else (lo,))
                ]
                path.moveTo(points[0])
                for point in points[1:]:
                    path.lineTo(point)
            p.drawPath(path)
        p.setClipping(False)

        ly = 14 + (metrics.height() + 8 if title else 0) + metrics.ascent()
        for entries in legend:
            lx = plot.left()
            for color, text in entries:
                p.setPen(QPen(QColor(color), 3))
                p.drawLine(
                    QPointF(lx, ly - metrics.ascent() / 3),
                    QPointF(lx + 22, ly - metrics.ascent() / 3),
                )
                p.setPen(QColor("#0f172a"))
                p.drawText(QPointF(lx + 28, ly), text)
                lx += 28 + metrics.horizontalAdvance(text) + 22
            ly += metrics.height() + 4
        p.end()
    return image


def _pixel_columns(s: Series, x0: float, x1: float, px) -> list[list[tuple[int, float, float]]]:
    """Reduces a series to per-pixel min and max segments."""
    pieces, piece = [], []
    for x, y in zip(s.x, s.y, strict=False):
        if not x0 <= x <= x1:
            continue
        if y is None or not math.isfinite(y):
            if piece:
                pieces.append(piece)
            piece = []
            continue
        column = round(px(x))
        if piece and piece[-1][0] == column:
            _, lo, hi = piece[-1]
            piece[-1] = (column, min(lo, y), max(hi, y))
        else:
            piece.append((column, y, y))
    if piece:
        pieces.append(piece)
    return pieces


def image_bytes(image: QImage, fmt: str = "png") -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, IMAGE_FORMATS[fmt])
    buffer.close()
    return bytes(data)


def save_image(image: QImage, path: Path, fmt: str) -> Path:
    fmt = fmt.lower()
    if fmt not in IMAGE_FORMATS:
        raise ExportError(f"Image format must be one of {', '.join(sorted(IMAGE_FORMATS))}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _paint_lock:
        if not image.save(
            str(path), IMAGE_FORMATS[fmt], 92 if IMAGE_FORMATS[fmt] == "JPEG" else -1
        ):
            raise ExportError(f"Could not write {path}.")
    return path


def _stages_and_markers(run: RunExport):
    a = run.analysis
    regions, markers = [], []
    if a is None:
        return regions, markers
    for stage in a.run.stages:
        s = stage.segment
        regions.append(
            Region(
                s.start,
                max(s.end, s.start + 1.0),
                STAGE_NAMES.get(s.kind, "Unknown"),
                STAGE_COLORS.get(s.kind, "#64748b"),
            )
        )
    for x in a.anomalies:
        if anomaly_mod.SEVERITY_RANK[x.severity] >= anomaly_mod.SEVERITY_RANK[anomaly_mod.WARNING]:
            markers.append(
                Marker(x.start, ANOMALY_NAMES.get(x.kind, x.kind), SEVERITY_COLORS[x.severity])
            )
    return regions, markers


CHART_KINDS = {
    "temperature": (
        ["temp", "temp_ref"],
        "Temperature and setpoint with stages and anomalies (warning or worse)",
    ),
    "control": (
        ["temp_u", "temp_u_p", "temp_u_i", "temp_u_d"],
        "Control output and its P / I / D terms",
    ),
}


def run_chart(
    run: RunExport,
    kind: str = "temperature",
    channels: list[str] | None = None,
    from_s: float | None = None,
    to_s: float | None = None,
    width: int = CHART_WIDTH,
    height: int = CHART_HEIGHT,
) -> tuple[QImage, str, list[str]]:
    """Renders a chart of a run."""
    if channels:
        names, caption = list(channels), "Channels " + ", ".join(channels)
    elif kind in CHART_KINDS:
        names, caption = CHART_KINDS[kind]
    else:
        raise ExportError(f"Chart kind must be one of {', '.join(CHART_KINDS)} (or give channels).")
    names = [c for c in names if c in run.channels]
    if not names:
        raise ExportError(f"{run.title} has none of the channels to draw.")
    order = list(run.channels)
    series = [
        Series(run.label(c), channel_color(c, order.index(c)), run.times, run.channels[c])
        for c in names
    ]
    regions, markers = _stages_and_markers(run)
    image = render_chart(
        series, regions, markers, f"{run.title}: {caption}", width, height, from_s, to_s
    )
    return image, caption, names


def comparison_chart(
    runs: list[RunExport],
    channel: str = "temp",
    width: int = CHART_WIDTH,
    height: int = CHART_HEIGHT,
) -> tuple[QImage, str]:
    """One channel of several runs, aligned on their start."""
    series = []
    for i, run in enumerate(runs):
        if channel in run.channels:
            series.append(
                Series(
                    run.title,
                    COMPARE_COLORS[i % len(COMPARE_COLORS)],
                    run.times,
                    run.channels[channel],
                )
            )
    if not series:
        raise ExportError(f"None of the runs has the channel {channel}.")
    label = runs[0].label(channel)
    caption = f"Comparison of {label}, aligned on the run start"
    return render_chart(series, title=caption, width=width, height=height), caption


def recorded_export(
    chamber: str,
    start_epoch: float,
    end_epoch: float,
    title: str | None = None,
    window=None,
) -> RunExport:
    """Builds an exportable run from a chamber's day recordings."""
    from app.api import control_settings
    from app.core.analysis import ExperimentInput, analyse_experiment
    from app.services import day_recorder

    if window is None:
        window = day_recorder.read_range(chamber, start_epoch, end_epoch)
    times, channels = window
    if len(times) == 0:
        raise ExportError(
            f"Nothing was recorded from {chamber} between {date_time(start_epoch)} and "
            f"{date_time(end_epoch)}."
        )
    first = float(times[0])
    elapsed = [float(t) - first for t in times]
    columns = {name: [float(v) for v in values] for name, values in channels.items()}
    try:
        analysis = analyse_experiment(ExperimentInput(elapsed, columns))
    except Exception:
        analysis = None
    meta = {}
    with contextlib.suppress(Exception):
        meta = control_settings.state_meta(control_settings.load())
    return RunExport(
        title=title
        or f"{chamber} {datetime.fromtimestamp(start_epoch):%Y-%m-%d %H:%M} – "
        f"{datetime.fromtimestamp(end_epoch):%Y-%m-%d %H:%M}",
        start_epoch=first,
        times=elapsed,
        channels=columns,
        analysis=analysis,
        labels={n: m.get("label", "") for n, m in meta.items() if n in columns},
        units={n: m.get("unit", "") for n, m in meta.items() if n in columns},
        chamber=chamber,
    )


def write_data(
    run: RunExport,
    path: Path,
    channels: list[str] | None = None,
    from_s: float | None = None,
    to_s: float | None = None,
    tab_separated: bool = False,
) -> int:
    """Writes a run's data file; returns the number of rows."""
    names = [c for c in (channels or list(run.channels)) if c in run.channels]
    lo = -math.inf if from_s is None else float(from_s)
    hi = math.inf if to_s is None else float(to_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter="\t" if tab_separated else ",")

        def field_text(text):
            return text.replace("\t", " ") if tab_separated else text

        writer.writerow(["Time", "Elapsed, s", *(field_text(run.label(c)) for c in names)])
        for i, t in enumerate(run.times):
            if not lo <= t <= hi:
                continue
            epoch = run.epoch(t)
            writer.writerow(
                [
                    date_time(epoch) if math.isfinite(epoch) else "",
                    _csv_number(t),
                    *(_csv_number(run.channels[c][i]) for c in names),
                ]
            )
            rows += 1
    return rows


def write_comparison_csv(runs: list[RunExport], path: Path, channel: str = "temp") -> int:
    """Writes the run comparison CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Experiment",
                "Start",
                "End",
                "Duration, s",
                "Samples",
                "Channel",
                "Min",
                "Max",
                "Mean",
                "Final",
            ]
        )
        for run in runs:
            values = [
                v for v in run.channels.get(channel, []) if v is not None and not math.isnan(v)
            ]
            writer.writerow(
                [
                    run.title,
                    date_time(run.epoch(run.times[0])) if run.times else "",
                    date_time(run.epoch(run.times[-1])) if run.times else "",
                    _csv_number(run.duration_s),
                    len(run.times),
                    runs[0].label(channel),
                    _csv_number(min(values)) if values else "",
                    _csv_number(max(values)) if values else "",
                    _csv_number(sum(values) / len(values)) if values else "",
                    _csv_number(values[-1]) if values else "",
                ]
            )
    return len(runs)


def write_bundle(run: RunExport, path: Path) -> Path:
    """Writes a run bundle."""
    bundle = dict(run.manifest or {"schema": "deepvac.run/1", "identity": {"name": run.title}})
    integrity = dict(bundle.get("integrity") or {})
    integrity["status_when_exported"] = "unknown"
    bundle["integrity"] = integrity
    bundle["store"] = {
        "title": run.title,
        "chamber": run.chamber,
        "start": run.start_epoch,
        "exported_by": "Deepvac Insight",
    }
    bundle["analysis"] = run.summary or {}
    bundle["samples"] = run.samples if run.samples is not None else []
    bundle["annotations"] = [
        {
            "start": a["x0"],
            "end": a["x1"],
            "label": a["label"],
            "user": a["user_name"],
            "created_at": a["created_at"],
        }
        for a in run.annotations
    ]
    bundle["variable_rules"] = [
        {
            "channel": r["channel"],
            "lo": r["lo"],
            "hi": r["hi"],
            "label": r["name"],
            "user": r["user_name"],
        }
        for r in run.rules
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding="utf-8",
    )
    return path


def _table(headers, rows) -> str:
    """Rows of HTML cells."""
    html = '<table border="1" cellspacing="0" cellpadding="3" width="100%"><tr>'
    html += "".join(f'<th bgcolor="#e2e8f0" align="left">{_esc(h)}</th>' for h in headers) + "</tr>"
    for row in rows:
        html += "<tr>" + "".join(f"<td>{cell or '&nbsp;'}</td>" for cell in row) + "</tr>"
    return html + "</table>"


def _facts(items) -> str:
    html = '<table border="1" cellspacing="0" cellpadding="3">'
    for key, value in items:
        html += f'<tr><td bgcolor="#f1f5f9"><b>{_esc(key)}</b></td><td>{_esc(value)}</td></tr>'
    return html + "</table>"


def _colored(text, color) -> str:
    return f'<font color="{color}">{_esc(text)}</font>'


def _job_text(configuration: dict) -> str:
    job = configuration.get("job") or {}
    if not job:
        return "not recorded"
    text = job.get("label") or ""
    profile = job.get("test_profile") or ""
    if profile and profile not in text:
        text += f' "{profile}"'
    kind = job.get("kind") or ""
    return f"{text or 'job'} ({JOB_KINDS.get(kind, kind)})"


def _captured_text(configuration: dict) -> str:
    if not configuration.get("captured_at_execution"):
        return "not captured at execution (experiment saved later, imported or recorded by an older version)"
    text = f"captured when the job was sent, {configuration.get('captured_at', '')}"
    if configuration.get("note"):
        text += f" ({configuration['note']})"
    return text


def _program_rows(configuration: dict) -> list[list[str]]:
    rows = []
    intervals = ((configuration.get("job") or {}).get("program") or {}).get("intervals") or []
    for i, interval in enumerate(intervals, start=1):
        flags = int(interval.get("flags") or 0)
        duration = float(interval.get("duration") or 0)
        mode = [
            m
            for bit, m in ((RAMP_BIT, "ramp"), (AWAIT_BIT, "wait for setpoint"))
            if flags >> bit & 1
        ]
        rows.append(
            [
                str(i),
                f"{interval['temp']:g} °C" if "temp" in interval else "—",
                "until stopped" if duration >= 99 * 3600 else seconds(duration),
                ", ".join(mode) or "step / hold",
            ]
        )
    return rows


def _pid_rows(configuration: dict) -> list[list[str]]:
    rows = []
    for loop in (configuration.get("plc_settings") or {}).get("pids") or []:
        points = int(loop.get("points") or 0)
        lo, hi = float(loop.get("min") or 0), float(loop.get("max") or 0)
        width = (hi - lo) / points if points else 0.0
        for band in range(points):
            gains = [loop.get(key) or [] for key in ("p", "i", "d")]
            rows.append(
                [
                    loop.get("label") or loop.get("name") or "",
                    str(band),
                    f"{number(lo + band * width, 6)} … {number(lo + (band + 1) * width, 6)}",
                    *(number(g[band], 6) if band < len(g) else "—" for g in gains),
                ]
            )
    return rows


def _calibration_summary(configuration: dict) -> str:
    parts = []
    for c in (configuration.get("plc_settings") or {}).get("calibrations") or []:
        points = ", ".join(
            f"{number(x, 6)}:{number(d, 6)}"
            for x, d in zip(c.get("x") or [], c.get("delta") or [], strict=False)
        )
        parts.append(f"{c.get('state_name') or c.get('name', '')} [{points}]")
    return "; ".join(parts) or "not recorded"


def comparison_html(runs: list[RunExport]) -> str:
    """Builds the comparison section of a report."""
    analyses = [r.analysis for r in runs]
    names = [_esc(r.title) for r in runs]
    rows = []
    for row in compare_kpis(analyses):
        if all(not math.isfinite(v) for v in row.values):
            continue
        ref = row.values[0]
        cells = [_esc(f"{row.label}{f' ({row.unit})' if row.unit else ''}"), number(ref)]
        for v in row.values[1:]:
            delta = f" ({v - ref:+.3g})" if math.isfinite(v) and math.isfinite(ref) else ""
            cells.append(number(v) + delta)
        rows.append(cells)
    html = _table(["KPI", *names], rows)
    config = [
        [_esc(r.label), *(_esc(v) for v in r.values)]
        for r in compare_configs([a.config for a in analyses])
        if r.differs
    ]
    if config:
        html += f"<h3>{_esc('Configuration that differs')}</h3>" + _table(
            ["Setting", *names], config
        )
    steps = []
    for k, match in enumerate(match_steps(analyses)[:10], start=1):
        cells = [str(k)]
        for a, idx in zip(analyses, match, strict=True):
            if idx < 0:
                cells.append("—")
            else:
                s = a.run.stages[idx]
                cells.append(
                    f"→ {number(s.segment.reference_end, 4)} · overshoot {number(s.kpis.overshoot, 3)} · "
                    f"settling {seconds(s.kpis.settling_time_s)}"
                )
        steps.append(cells)
    if steps:
        html += f"<h3>{_esc('Matching step responses')}</h3>" + _table(["Step", *names], steps)
    return html


def report_html(
    run: RunExport,
    charts: list[tuple[str, QImage]],
    embed_images: bool,
    author: str = "",
    comparison: str = "",
) -> str:
    a = run.analysis
    manifest = run.manifest or {}
    body = []

    def section(title):
        body.append(f"<h2>{_esc(title)}</h2>")

    body.append(f"<h1>{_esc('Experiment report: ' + run.title)}</h1>")
    generated = (
        f"Generated {datetime.now():%Y-%m-%d %H:%M}"
        + (f" for {author}" if author else "")
        + " by Deepvac Insight"
    )
    body.append(f'<p><font color="#64748b">{_esc(generated)}</font></p>')

    section("Summary")
    summary = [
        ("Start", date_time(run.epoch(run.times[0])) if run.times else "—"),
        ("End", date_time(run.epoch(run.times[-1])) if run.times else "—"),
        ("Duration", seconds(run.duration_s)),
        ("Samples", str(len(run.times))),
    ]
    if run.chamber:
        summary.append(("Chamber", run.chamber))
    if a is not None:
        critical = a.anomaly_count(anomaly_mod.CRITICAL)
        warnings = a.anomaly_count(anomaly_mod.WARNING) - critical
        info = len(a.anomalies) - a.anomaly_count(anomaly_mod.WARNING)
        summary.append(("Anomalies", f"{critical} critical, {warnings} warnings, {info} info"))
    body.append(_facts(summary))

    section("Experiment details")
    details = manifest.get("metadata") or {}

    def or_dash(value):
        return value if value not in (None, "", []) else "—"

    detail_facts = [
        ("DUT", or_dash(details.get("dut"))),
        ("DUT mass", or_dash(details.get("dut_mass"))),
        ("DUT density", or_dash(details.get("dut_density"))),
        ("Operator", or_dash(details.get("operator"))),
        ("Purpose", or_dash(details.get("purpose"))),
        ("Tags", or_dash(", ".join(details.get("tags") or []))),
    ]
    if details.get("notes"):
        detail_facts.append(("Notes", details["notes"]))
    identity = manifest.get("identity") or {}
    integrity = manifest.get("integrity") or {}
    if manifest:
        app = identity.get("app") or {}
        detail_facts += [
            ("Experiment uid", or_dash(identity.get("run_uid"))),
            ("Recorded by", or_dash(f"{app.get('name', '')} {app.get('version', '')}".strip())),
            ("Time resolution", f"{identity.get('time_resolution_s', '—')} s"),
            ("Samples SHA-256", or_dash(integrity.get("samples_sha256"))),
        ]
    else:
        detail_facts.append(
            ("Record", "no manifest: recorded outside Deepvac Control or before manifests")
        )
    body.append(_facts(detail_facts))

    if a is not None and a.has_control:
        k = a.run.overall
        section("Control KPIs (temp vs temp_ref)")
        body.append(
            _facts(
                [
                    ("MAE", number(k.mae)),
                    ("RMSE", number(k.rmse)),
                    ("Max |error|", number(k.max_abs_error)),
                    ("Overshoot (worst step)", number(a.run.overshoot)),
                    ("Undershoot (worst step)", number(a.run.undershoot)),
                    ("Settling of the last setpoint", seconds(a.run.settling_time_s)),
                    ("Steady-state error", number(k.steady_state_error)),
                    ("Tail MAE / std", f"{number(k.tail_mae)} / {number(k.tail_std)}"),
                    ("Control cost", number(a.run.cost)),
                    ("Output saturated", percent(a.saturation.fraction)),
                    ("Integrator windup", seconds(a.saturation.windup_s)),
                    ("P / I / D share", pid_shares(a.pid)),
                ]
            )
        )
    else:
        body.append(f"<p>{_esc('No temp / temp_ref recorded: no control KPIs or stages.')}</p>")

    if a is not None:
        c = a.config
        section("Configuration")
        body.append(
            _facts(
                [
                    ("Sample period", f"{number(c.sample_period_s, 3)} s"),
                    (
                        "PID gains (most used)",
                        f"Kp {number(c.kp)} · Ki {number(c.ki)} · Kd {number(c.kd)}",
                    ),
                    ("PID gain sets used", str(c.gain_sets)),
                    ("Setpoints", " → ".join(number(s, 2) for s in c.setpoints) or "—"),
                    (
                        "Reference range",
                        f"{number(c.min_reference, 2)} … {number(c.max_reference, 2)}",
                    ),
                    ("Max ramp rate", f"{number(c.max_ramp_rate_per_min, 3)} /min"),
                    (
                        "Stages (step / ramp / hold)",
                        f"{c.stages} ({c.steps} / {c.ramps} / {c.holds})",
                    ),
                    (
                        "Program intervals / cycles",
                        f"{c.program_intervals or '—'} / {c.cycles or '—'}",
                    ),
                    ("Recorded channels", ", ".join(c.channels)),
                ]
            )
        )

    configuration = manifest.get("configuration") or {}
    if configuration:
        section("Configuration snapshot")
        app = configuration.get("app") or {}
        body.append(
            _facts(
                [
                    ("Captured", _captured_text(configuration)),
                    ("Job", _job_text(configuration)),
                    ("User", or_dash((configuration.get("user") or {}).get("name"))),
                    (
                        "Application",
                        or_dash(f"{app.get('name', '')} {app.get('version', '')}".strip()),
                    ),
                    ("Calibration", _calibration_summary(configuration)),
                ]
            )
        )
        program = _program_rows(configuration)
        if program:
            body.append(f"<h3>{_esc('Program sent to the PLC')}</h3>")
            body.append(
                _table(
                    ["#", "Setpoint", "Duration", "Mode"], [[_esc(c) for c in r] for r in program]
                )
            )
        pid = _pid_rows(configuration)
        if pid:
            body.append(f"<h3>{_esc('PID table')}</h3>")
            body.append(
                _table(
                    ["Loop", "Band", "Range", "Kp", "Ki", "Kd"], [[_esc(c) for c in r] for r in pid]
                )
            )
    described = [c for c in manifest.get("channels") or [] if c.get("name") in run.channels]
    if described:
        section("Channels")
        body.append(
            _table(
                ["Name", "Description", "Unit", "Quantity", "Role"],
                [
                    [_esc(c.get(k, "")) for k in ("name", "label", "unit", "quantity", "role")]
                    for c in described
                ],
            )
        )

    if charts:
        section("Charts")
        for i, (caption, image) in enumerate(charts):
            src = (
                "data:image/png;base64," + base64.b64encode(image_bytes(image)).decode("ascii")
                if embed_images
                else f"chart{i}.png"
            )
            body.append(
                f'<p><img src="{src}" width="680"><br><font color="#64748b">{_esc(caption)}</font></p>'
            )

    if a is not None and a.run.stages:
        section("Stages: control KPIs")
        kpi_rows, behaviour_rows = [], []
        for r, (stage, d) in enumerate(zip(a.run.stages, a.stage_details, strict=False), start=1):
            s = stage.segment
            step, ramp = s.kind == "setpoint", s.kind == "ramp"
            setpoint = (
                number(s.reference_end, 2)
                if abs(s.reference_end - s.reference_start) < 0.05
                else f"{number(s.reference_start, 2)} → {number(s.reference_end, 2)}"
            )
            k = stage.kpis
            transition = k.rise_time_s if math.isfinite(k.rise_time_s) else k.fall_time_s
            name = _colored(STAGE_NAMES.get(s.kind, "Unknown"), STAGE_COLORS.get(s.kind, "#64748b"))
            kpi_rows.append(
                [
                    str(r),
                    name,
                    str(s.interval) if s.interval >= 0 else "—",
                    seconds(s.start),
                    seconds(s.end - s.start),
                    _esc(setpoint),
                    number(k.mae, 3),
                    number(k.overshoot, 3) if step else "—",
                    seconds(k.settling_time_s) if step else "—",
                    seconds(transition) if step else "—",
                    number(k.steady_state_error, 3) if step else "—",
                ]
            )
            behaviour_rows.append(
                [
                    str(r),
                    name,
                    seconds(d.time_to_steady_s),
                    percent(d.steady_fraction),
                    f"{number(d.reference_rate_per_min, 3)} / {number(d.value_rate_per_min, 3)}"
                    if ramp
                    else "—",
                    seconds(d.tracking_lag_s) if ramp else "—",
                    percent(d.saturation.fraction),
                    seconds(d.saturation.windup_s),
                    _esc(pid_shares(d.pid)),
                    f"{number(d.kp)} / {number(d.ki)} / {number(d.kd)}",
                ]
            )
        body.append(
            _table(
                [
                    "#",
                    "Stage",
                    "Interval",
                    "Start",
                    "Duration",
                    "Setpoint",
                    "MAE",
                    "Overshoot",
                    "Settling",
                    "Rise / fall",
                    "Steady-state error",
                ],
                kpi_rows,
            )
        )
        section("Stages: behaviour")
        body.append(
            _table(
                [
                    "#",
                    "Stage",
                    "Steady after",
                    "Time steady",
                    "Rate ref / value, /min",
                    "Tracking lag",
                    "Output saturated",
                    "Windup",
                    "P / I / D share",
                    "Kp / Ki / Kd",
                ],
                behaviour_rows,
            )
        )

    if a is not None and a.steady:
        section("Steady states")
        body.append(
            _table(
                ["Start", "Duration", "Mean", "Std", "Mean error"],
                [
                    [
                        seconds(s.start),
                        seconds(s.end - s.start),
                        number(s.mean, 3),
                        number(s.std, 4),
                        number(s.mean_error, 3),
                    ]
                    for s in a.steady
                ],
            )
        )

    if a is not None:
        section("Anomalies")
        if not a.anomalies:
            body.append("<p>None detected.</p>")
        else:
            body.append(
                _table(
                    ["Severity", "Type", "Channel", "Start", "Duration", "Detail"],
                    [
                        [
                            _colored(x.severity.capitalize(), SEVERITY_COLORS[x.severity]),
                            _esc(ANOMALY_NAMES.get(x.kind, x.kind)),
                            _esc(x.channel),
                            seconds(x.start),
                            seconds(x.end - x.start),
                            _esc(x.detail),
                        ]
                        for x in a.anomalies
                    ],
                )
            )

    events = manifest.get("events") or []
    if events:
        section("Events")
        start = run.epoch(run.times[0]) if run.times else math.nan
        rows = []
        for e in events[:MAX_EVENTS]:
            t = e.get("t")
            etype = e.get("type", "")
            rows.append(
                [
                    seconds(t - start) if t is not None and math.isfinite(start) else "—",
                    date_time(t),
                    _colored(EVENT_NAMES.get(etype, etype), _event_color(etype)),
                    "live" if e.get("source") == "app" else "signal",
                    _esc(e.get("message", "")),
                ]
            )
        body.append(_table(["Elapsed", "Time", "Event", "Source", "Details"], rows))
        if len(events) > MAX_EVENTS:
            body.append(f"<p>… and {len(events) - MAX_EVENTS} more</p>")

    if run.annotations or run.rules:
        section("Notes")
        rows = [
            [
                "Annotation",
                _esc(n["label"]),
                f"{seconds(n['x0'])} – {seconds(n['x1'])}",
                _esc(n["user_name"] or ""),
            ]
            for n in run.annotations
        ]
        rows += [
            [
                "Rule",
                _esc(r["name"]),
                f"{_esc(r['channel'])} in [{'−∞' if r['lo'] is None else number(r['lo'])}, "
                f"{'+∞' if r['hi'] is None else number(r['hi'])}]",
                _esc(r["user_name"] or ""),
            ]
            for r in run.rules
        ]
        body.append(_table(["Kind", "Label", "Where", "User"], rows))

    if comparison:
        section("Comparison")
        body.append(comparison)

    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{_esc(run.title)}</title>'
        "<style>body{font-family:'Segoe UI',Arial,sans-serif;font-size:10pt;color:#0f172a;max-width:1100px;margin:16px;}"
        "h1{font-size:16pt;}h2{font-size:13pt;margin-top:18px;border-bottom:1px solid #cbd5e1;}h3{font-size:11pt;}"
        "table{border-collapse:collapse;margin:4px 0;}td,th{border:1px solid #cbd5e1;padding:3px 6px;font-size:9pt;}"
        "th{background:#e2e8f0;text-align:left;}</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )


def _event_color(etype: str) -> str:
    if etype.startswith("alarm") or etype == "app_error":
        return "#dc2626"
    if etype.startswith(("setpoint", "ramp")):
        return "#2563eb"
    if etype.startswith("pid"):
        return "#d97706"
    if etype.startswith(("job", "operator")) or etype == "user_change":
        return "#16a34a"
    return "#64748b"


def report_charts(
    run: RunExport, compare: list[RunExport] = (), compare_channel: str = "temp"
) -> list[tuple[str, QImage]]:
    """Renders the charts of a run report."""
    charts = []
    if compare:
        image, caption = comparison_chart([run, *compare], compare_channel)
        charts.append((caption, image))
    for kind in CHART_KINDS:
        try:
            image, caption, _ = run_chart(run, kind)
        except ExportError:
            continue
        charts.append((caption, image))
    return charts


def write_report(
    run: RunExport,
    path: Path,
    fmt: str = "pdf",
    compare: list[RunExport] = (),
    author: str = "",
    first_charts: list[tuple[str, QImage]] = (),
    compare_channel: str = "temp",
) -> Path:
    """Writes a run report."""
    fmt = fmt.lower()
    if fmt not in REPORT_FORMATS:
        raise ExportError("Report format must be pdf or html.")
    charts = [*first_charts, *report_charts(run, compare, compare_channel)]
    comparison = comparison_html([run, *compare]) if compare else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "html":
        path.write_text(report_html(run, charts, True, author, comparison), encoding="utf-8")
        return path
    with _paint_lock:
        document = QTextDocument()
        for i, (_, image) in enumerate(charts):
            document.addResource(QTextDocument.ImageResource, QUrl(f"chart{i}.png"), image)
        document.setHtml(report_html(run, charts, False, author, comparison))
        writer = QPdfWriter(str(path))
        writer.setPageLayout(
            QPageLayout(
                QPageSize(QPageSize.A4),
                QPageLayout.Landscape,
                QMarginsF(12, 12, 12, 12),
                QPageLayout.Millimeter,
            )
        )
        writer.setTitle(run.title)
        writer.setCreator("Deepvac Insight")
        document.print_(writer)
        del writer
    if not path.exists() or path.stat().st_size == 0:
        raise ExportError(f"Could not write {path}.")
    return path
