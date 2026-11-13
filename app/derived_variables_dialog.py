"""Dialog to manage derived-variable definitions."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.common import COLORS
from app.services import derived_variables_service as dv


class DerivedVariablesDialog(QDialog):
    def __init__(self, available_columns, current_user=None, parent=None):
        super().__init__(parent)
        self.available_columns = list(available_columns)
        self.current_user = current_user or {"name": "Unknown"}
        self.changed = False

        self.setWindowTitle(self.tr("Manage Derived Variables"))
        self.setMinimumWidth(480)
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        root = QVBoxLayout(self)

        info = QLabel(
            self.tr(
                "Derived variables are reusable formulas -- once created, they show up "
                "as extra channels for any run that has the source channel(s) they need."
            )
        )
        info.setWordWrap(True)
        root.addWidget(info)

        self._list = QListWidget()
        self._list.setMinimumHeight(140)
        root.addWidget(self._list)

        form = QFormLayout()
        self._name_ed = QLineEdit()
        self._name_ed.setPlaceholderText(self.tr("e.g. temperature_error"))
        form.addRow(self.tr("Name"), self._name_ed)

        self._type_combo = QComboBox()
        self._type_combo.addItem(self.tr("Difference (A − B)"), dv.TYPE_DIFFERENCE)
        self._type_combo.addItem(self.tr("Rate of change (dA/dt)"), dv.TYPE_RATE_OF_CHANGE)
        self._type_combo.addItem(self.tr("Rolling standard deviation"), dv.TYPE_ROLLING_STD)
        self._type_combo.addItem(
            self.tr("Cumulative integral (∫A dt)"), dv.TYPE_CUMULATIVE_INTEGRAL
        )
        self._type_combo.addItem(self.tr("Custom expression"), dv.TYPE_CUSTOM)
        self._type_combo.currentIndexChanged.connect(self._update_form_visibility)
        form.addRow(self.tr("Type"), self._type_combo)

        self._source_combo = QComboBox()
        self._source_combo.addItems(self.available_columns)
        self._source_row_label = QLabel(self.tr("Source channel"))
        form.addRow(self._source_row_label, self._source_combo)

        self._source2_combo = QComboBox()
        self._source2_combo.addItems(self.available_columns)
        self._source2_row_label = QLabel(self.tr("Source channel 2"))
        form.addRow(self._source2_row_label, self._source2_combo)

        self._window_spin = QSpinBox()
        self._window_spin.setRange(2, 10_000)
        self._window_spin.setValue(10)
        self._window_row_label = QLabel(self.tr("Window (samples)"))
        form.addRow(self._window_row_label, self._window_spin)

        self._expression_ed = QLineEdit()
        self._expression_ed.setPlaceholderText(self.tr("e.g. temp_u_p + temp_u_i + temp_u_d"))
        self._expression_row_label = QLabel(self.tr("Expression"))
        form.addRow(self._expression_row_label, self._expression_ed)

        color_row = QWidget()
        color_lay = QHBoxLayout(color_row)
        color_lay.setContentsMargins(0, 0, 0, 0)
        self._color = COLORS[0]
        self._color_swatch = QPushButton()
        self._color_swatch.setFixedSize(20, 20)
        self._update_color_swatch()
        self._color_swatch.clicked.connect(self._pick_color)
        color_lay.addWidget(self._color_swatch)
        color_lay.addStretch(1)
        form.addRow(self.tr("Color"), color_row)

        root.addLayout(form)

        button_row = QHBoxLayout()
        add_btn = QPushButton(self.tr("Add"))
        add_btn.setObjectName("primaryButton")
        add_btn.clicked.connect(self._add)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(add_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

        self._update_form_visibility()

    def _update_color_swatch(self):
        self._color_swatch.setStyleSheet(
            f"background:{self._color};border:1px solid rgba(255,255,255,0.25);border-radius:3px;"
        )

    def _pick_color(self):
        picked = QColorDialog.getColor(QColor(self._color), self, self.tr("Color"))
        if picked.isValid():
            self._color = picked.name()
            self._update_color_swatch()

    def _update_form_visibility(self):
        var_type = self._type_combo.currentData()
        is_difference = var_type == dv.TYPE_DIFFERENCE
        needs_source = var_type in (
            dv.TYPE_DIFFERENCE,
            dv.TYPE_RATE_OF_CHANGE,
            dv.TYPE_ROLLING_STD,
            dv.TYPE_CUMULATIVE_INTEGRAL,
        )
        needs_window = var_type == dv.TYPE_ROLLING_STD
        needs_expression = var_type == dv.TYPE_CUSTOM

        self._source_row_label.setVisible(needs_source)
        self._source_combo.setVisible(needs_source)
        self._source2_row_label.setVisible(is_difference)
        self._source2_combo.setVisible(is_difference)
        self._window_row_label.setVisible(needs_window)
        self._window_spin.setVisible(needs_window)
        self._expression_row_label.setVisible(needs_expression)
        self._expression_ed.setVisible(needs_expression)

    def _refresh_list(self):
        self._list.clear()
        for definition in dv.list_derived_variables():
            detail = definition.get("expression") or definition.get("source_channel") or ""
            item = QListWidgetItem(f"{definition['name']}  ({definition['type']}: {detail})")
            item.setData(Qt.UserRole, definition["id"])
            self._list.addItem(item)
        self._list.itemDoubleClicked.connect(self._delete_selected)

    def _add(self):
        var_type = self._type_combo.currentData()
        try:
            dv.add_derived_variable(
                self._name_ed.text().strip(),
                var_type,
                source_channel=self._source_combo.currentText() or None,
                source_channel2=self._source2_combo.currentText() or None,
                window=self._window_spin.value(),
                expression=self._expression_ed.text().strip() or None,
                color=self._color,
                created_by=self.current_user.get("name") or "Unknown",
            )
        except dv.DerivedVariableError as exc:
            QMessageBox.critical(self, self.tr("Could not add derived variable"), str(exc))
            return
        self._name_ed.clear()
        self._expression_ed.clear()
        self.changed = True
        self._refresh_list()

    def _delete_selected(self, item):
        variable_id = item.data(Qt.UserRole)
        dv.delete_derived_variable(variable_id)
        self.changed = True
        self._refresh_list()
