"""Program library of the Controller page."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.program_editor import duration_text
from app.program_model import AWAIT_BIT, ProgramModel, bit, set_bit
from app.services import test_profiles_service as profiles


def _tr(text):
    return QCoreApplication.translate("ProgramLibrary", text)


def steps_from_model(model: ProgramModel, labels: list[str]) -> list[dict]:
    temp_row, pres_row = model.row_of("temp"), model.row_of("pres")
    steps = []
    for i in range(model.count()):
        flags = model.flags(i)
        steps.append(
            {
                "duration_s": float(model.value(i, "duration")),
                "setpoint_temp": float(model.value(i, "temp")) if bit(flags, temp_row) else None,
                "setpoint_pressure": float(model.value(i, "pres"))
                if bit(flags, pres_row)
                else None,
                "label": labels[i] if i < len(labels) else "",
            }
        )
    return steps


def load_into_model(profile: dict, model: ProgramModel) -> str:
    """Loads a saved program into the model."""
    program = profile.get("program")
    if program:
        if model.set_program(program):
            return ""
        return _tr("'{0}' was saved for a chamber with other control channels.").format(
            profile["name"]
        )
    temp_row, pres_row = model.row_of("temp"), model.row_of("pres")
    defaults = {name: model.param(row).get("value", 0) for row, name in enumerate(model.order)}
    intervals = []
    for i, step in enumerate(profile.get("steps") or [], start=1):
        temp, pres = step.get("setpoint_temp"), step.get("setpoint_pressure")
        if (temp is not None and temp_row < 0) or (pres is not None and pres_row < 0):
            return _tr("Step {0} uses a setpoint this chamber cannot control.").format(i)
        interval = dict(defaults)
        flags = 1 << AWAIT_BIT
        if temp is not None:
            interval["temp"] = float(temp)
            flags = set_bit(flags, temp_row, True)
        if pres is not None:
            interval["pres"] = float(pres)
            flags = set_bit(flags, pres_row, True)
        interval["duration"] = int(round(float(step["duration_s"])))
        interval["flags"] = flags
        intervals.append(interval)
    if not intervals or not model.set_program(
        {"intervals": intervals, "cycle": {"start": 0, "end": 0, "count": 0}}
    ):
        return _tr("'{0}' has no steps this chamber can run.").format(profile["name"])
    return ""


def setpoints_text(step: dict) -> str:
    parts = []
    if step.get("setpoint_temp") is not None:
        parts.append(_tr("{0:g} °C").format(step["setpoint_temp"]))
    if step.get("setpoint_pressure") is not None:
        parts.append(_tr("pressure {0:g}").format(step["setpoint_pressure"]))
    return ", ".join(parts) if parts else _tr("other channels")


def _profile_duration(profile):
    return sum(float(s["duration_s"]) for s in profile.get("steps") or [])


class ProgramLibraryDialog(QDialog):
    """Pick a saved program to open, or delete one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Program library"))
        self.resize(900, 560)
        self.selected = None
        root = QVBoxLayout(self)
        body = QHBoxLayout()
        self.list = QListWidget(self)
        self.preview = QLabel(self)
        self.preview.setWordWrap(True)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.preview.setMinimumWidth(320)
        body.addWidget(self.list, 3)
        body.addWidget(self.preview, 2)
        root.addLayout(body, 1)
        buttons = QHBoxLayout()
        self.open_btn = QPushButton(self.tr("Open"), self)
        self.open_btn.setObjectName("primaryButton")
        self.delete_btn = QPushButton(self.tr("Delete"), self)
        close = QPushButton(self.tr("Close"), self)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self.list.currentRowChanged.connect(self._show_preview)
        self.list.itemDoubleClicked.connect(lambda _item: self.open_btn.click())
        close.clicked.connect(self.reject)
        self.open_btn.clicked.connect(self._open)
        self.delete_btn.clicked.connect(self._delete)
        self.refresh()

    def refresh(self):
        self.list.clear()
        try:
            items = profiles.list_profiles()
        except Exception:
            items = []
        for profile in items:
            n = len(profile["steps"])
            item = QListWidgetItem(
                self.tr("{0}   ({1} interval(s), {2})").format(
                    profile["name"], n, duration_text(_profile_duration(profile))
                ),
                self.list,
            )
            item.setData(Qt.ItemDataRole.UserRole, profile["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, profile["name"])
        if self.list.count():
            self.list.setCurrentRow(0)
        self._show_preview()

    def _current_profile(self):
        item = self.list.currentItem()
        return profiles.get_profile(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _show_preview(self, *_):
        item = self.list.currentItem()
        self.open_btn.setEnabled(item is not None)
        self.delete_btn.setEnabled(item is not None)
        if item is None:
            self.preview.setText(
                self.tr('The library is empty. Use "Save to library" on the Controller page.')
            )
            return
        profile = self._current_profile()
        if profile is None:
            return
        esc = lambda t: (t or "").replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
        lines = [
            f"<b>{esc(profile['name'])}</b>",
            self.tr("by {0}, created {1}").format(
                esc(profile.get("created_by")),
                (profile.get("created_at") or "")[:16].replace("T", " "),
            ),
            "",
        ]
        for i, step in enumerate(profile["steps"], start=1):
            label = f" · <i>{esc(step['label'])}</i>" if step.get("label") else ""
            lines.append(
                f"{i}. {duration_text(step['duration_s'])} · {esc(setpoints_text(step))}{label}"
            )
        if not profile.get("program"):
            lines += [
                "",
                self.tr("Saved as steps only: each step holds its setpoint for its duration."),
            ]
        self.preview.setText("<br>".join(lines))

    def _open(self):
        self.selected = self._current_profile()
        if self.selected is not None:
            self.accept()

    def _delete(self):
        item = self.list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole + 1)
        answer = QMessageBox.question(
            self, self.tr("Delete"), self.tr("Delete '{0}' from the library?").format(name)
        )
        if answer == QMessageBox.StandardButton.Yes:
            profiles.delete_profile(item.data(Qt.ItemDataRole.UserRole))
            self.refresh()


class ProgramSaveDialog(QDialog):
    """Save the program being edited to the library under a name, with a label per interval."""

    def __init__(self, model: ProgramModel, name, labels, user_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Save to library"))
        self.resize(820, 560)
        self.model = model
        self.user_name = user_name or "Unknown"
        self.saved = None
        root = QVBoxLayout(self)
        name_row = QHBoxLayout()
        self.name_edit = QLineEdit(name or "", self)
        self.name_edit.setPlaceholderText(self.tr("e.g. Thermal cycle A"))
        name_row.addWidget(QLabel(self.tr("Name"), self))
        name_row.addWidget(self.name_edit, 1)
        root.addLayout(name_row)
        root.addWidget(
            QLabel(self.tr("Interval labels (optional, shown while the program runs):"), self)
        )
        steps = steps_from_model(model, labels)
        self.table = QTableWidget(len(steps), 3, self)
        self.table.setHorizontalHeaderLabels(
            [self.tr("Duration"), self.tr("Setpoints"), self.tr("Label")]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(40)
        for row, step in enumerate(steps):
            for col, text in enumerate((duration_text(step["duration_s"]), setpoints_text(step))):
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, col, cell)
            label = QLineEdit(step["label"])
            self.table.setCellWidget(row, 2, label)
        root.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        save = QPushButton(self.tr("Save"), self)
        save.setObjectName("primaryButton")
        cancel = QPushButton(self.tr("Cancel"), self)
        buttons.addWidget(save)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        root.addLayout(buttons)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.save)

    def labels(self):
        return [self.table.cellWidget(r, 2).text().strip() for r in range(self.table.rowCount())]

    def save(self):
        name = self.name_edit.text().strip()
        steps = steps_from_model(self.model, self.labels())
        program = self.model.program()
        existing = profiles.find_by_name(name) if name else None
        if existing is not None:
            answer = QMessageBox.question(
                self,
                self.tr("Save to library"),
                self.tr("'{0}' already exists. Replace it?").format(name),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            if existing is not None:
                self.saved = profiles.update_profile(
                    existing["id"], name, existing.get("description", ""), steps, program=program
                )
            else:
                self.saved = profiles.add_profile(
                    name, "", steps, created_by=self.user_name, program=program
                )
        except profiles.TestProfileError as exc:
            QMessageBox.warning(self, self.tr("Could not save the program"), str(exc))
            return
        self.accept()
