"""Campaigns page."""

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.api import control_settings
from app.services import campaign_report, chambers_service
from app.services import campaign_service as cs

REFRESH_MS = 2000
STAGE_NAMES = {
    "requirement": "Requirement",
    "plan": "Plan",
    "approval": "Approval",
    "run": "Run",
    "report": "Report",
}
STATE_NAMES = {
    "draft": "Draft",
    "approval": "Waiting for approval",
    "running": "Running",
    "holding": "Decision point",
    "completed": "Completed",
    "aborted": "Aborted",
    "declined": "Declined",
    "cancelled": "Cancelled",
}


class NewCampaignDialog(QDialog):
    """Dialog for a new campaign's requirement."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("New test campaign"))
        form = QFormLayout(self)
        self.article = QLineEdit()
        self.article.setPlaceholderText(self.tr("e.g. 6U CubeSat, star tracker unit"))
        self.op_min = QDoubleSpinBox()
        self.op_max = QDoubleSpinBox()
        for box, value in ((self.op_min, -10.0), (self.op_max, 45.0)):
            box.setRange(-100.0, 200.0)
            box.setDecimals(1)
            box.setSuffix(" °C")
            box.setValue(value)
        self.level = QComboBox()
        for level in cs.LEVELS:
            self.level.addItem(self.tr(level.capitalize()), level)
        self.chamber = QComboBox()
        for chamber in chambers_service.list_chambers():
            if chamber["protocol"] == "plc":
                self.chamber.addItem(chamber["name"], chamber)
        self.rehearsal = QCheckBox(self.tr("Rehearsal (short dwells, to try the campaign out)"))
        self.notes = QLineEdit()
        form.addRow(self.tr("Test article"), self.article)
        form.addRow(self.tr("Lowest predicted temperature"), self.op_min)
        form.addRow(self.tr("Highest predicted temperature"), self.op_max)
        form.addRow(self.tr("Test level"), self.level)
        form.addRow(self.tr("Chamber"), self.chamber)
        form.addRow("", self.rehearsal)
        form.addRow(self.tr("Notes"), self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr("Draft the plan"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def requirement(self):
        return {
            "article": self.article.text(),
            "op_min_c": self.op_min.value(),
            "op_max_c": self.op_max.value(),
            "level": self.level.currentData(),
            "rehearsal": self.rehearsal.isChecked(),
            "notes": self.notes.text(),
        }


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.NoSelection)
    table.setWordWrap(True)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeToContents)
    header.setStretchLastSection(True)
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return table


def _fill(table, rows):
    """Fills a table when its rows changed."""
    key = repr(rows)
    if getattr(table, "_rows_key", None) == key:
        return
    table._rows_key = key
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(str(value)))
    table.resizeRowsToContents()
    height = (
        table.horizontalHeader().sizeHint().height()
        + table.verticalHeader().length()
        + 2 * table.frameWidth()
    )
    table.setFixedHeight(height + 2)


class CampaignsPage(QWidget):
    def __init__(self, current_user, supervisor_running=None, parent=None):
        """Campaigns page."""
        super().__init__(parent)
        self.setObjectName("workspaceBody")
        self.current_user = current_user or {}
        self._supervisor_running = supervisor_running or (lambda: True)
        self._selected = None
        self._shown_hold = None
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)

    @property
    def user_name(self):
        return self.current_user.get("name") or self.current_user.get("username") or ""

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)
        top = QHBoxLayout()
        title = QLabel(self.tr("Test campaigns"))
        title.setObjectName("pageTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.supervisor_label = QLabel()
        self.supervisor_label.setObjectName("sectionLabel")
        top.addWidget(self.supervisor_label)
        self.new_btn = QPushButton(self.tr("New campaign…"))
        self.new_btn.setObjectName("primaryButton")
        self.new_btn.clicked.connect(self.new_campaign)
        top.addWidget(self.new_btn)
        outer.addLayout(top)

        split = QHBoxLayout()
        split.setSpacing(12)
        list_card = QFrame()
        list_card.setObjectName("card")
        list_card.setFixedWidth(300)
        lc = QVBoxLayout(list_card)
        lc.setContentsMargins(10, 10, 10, 10)
        label = QLabel(self.tr("CAMPAIGNS"))
        label.setObjectName("sectionLabel")
        lc.addWidget(label)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        lc.addWidget(self.list, 1)
        split.addWidget(list_card)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        detail = QWidget()
        detail.setObjectName("workspaceBody")
        d = QVBoxLayout(detail)
        d.setContentsMargins(0, 0, 8, 0)
        d.setSpacing(10)
        self.title_label = QLabel(self.tr("No campaign selected"))
        self.title_label.setObjectName("title")
        d.addWidget(self.title_label)
        self.meta_label = QLabel()
        self.meta_label.setWordWrap(True)
        d.addWidget(self.meta_label)
        rail = QHBoxLayout()
        self.rail = {}
        for key, name in STAGE_NAMES.items():
            stage = QLabel(self.tr(name))
            stage.setAlignment(Qt.AlignCenter)
            stage.setMinimumWidth(90)
            rail.addWidget(stage)
            self.rail[key] = stage
        rail.addStretch(1)
        d.addLayout(rail)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        d.addWidget(self.status_label)

        self.hold_card = QFrame()
        self.hold_card.setObjectName("card")
        hc = QVBoxLayout(self.hold_card)
        hc.setContentsMargins(14, 12, 14, 12)
        tag = QLabel(self.tr("HOLD POINT: THE CAMPAIGN WAITS FOR YOUR DECISION"))
        tag.setObjectName("sectionLabel")
        hc.addWidget(tag)
        self.hold_title = QLabel()
        self.hold_title.setWordWrap(True)
        self.hold_title.setStyleSheet("font-weight: 700; font-size: 11pt;")
        hc.addWidget(self.hold_title)
        self.hold_detail = QLabel()
        self.hold_detail.setWordWrap(True)
        hc.addWidget(self.hold_detail)
        self.hold_recommendation = QLabel()
        self.hold_recommendation.setWordWrap(True)
        hc.addWidget(self.hold_recommendation)
        self.hold_comment = QLineEdit()
        self.hold_comment.setPlaceholderText(self.tr("Comment (optional, goes into the log)"))
        hc.addWidget(self.hold_comment)
        self.hold_options = QVBoxLayout()
        hc.addLayout(self.hold_options)
        d.addWidget(self.hold_card)

        actions = QHBoxLayout()
        self.submit_btn = QPushButton(self.tr("Submit for approval"))
        self.submit_btn.clicked.connect(self.submit)
        self.cancel_btn = QPushButton(self.tr("Cancel campaign"))
        self.cancel_btn.clicked.connect(self.cancel)
        self.stop_btn = QPushButton(self.tr("Stop now"))
        self.stop_btn.clicked.connect(self.stop_now)
        self.report_btn = QPushButton(self.tr("Issue report"))
        self.report_btn.clicked.connect(self.issue_report)
        self.open_btn = QPushButton(self.tr("Open report"))
        self.open_btn.clicked.connect(self.open_report)
        self.sign_btn = QPushButton(self.tr("Sign off"))
        self.sign_btn.clicked.connect(self.sign_off)
        self.verify_btn = QPushButton(self.tr("Verify log"))
        self.verify_btn.clicked.connect(self.verify_log)
        for button in (
            self.submit_btn,
            self.cancel_btn,
            self.stop_btn,
            self.report_btn,
            self.open_btn,
            self.sign_btn,
            self.verify_btn,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        d.addLayout(actions)
        self.verify_label = QLabel()
        self.verify_label.setWordWrap(True)
        self.verify_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        d.addWidget(self.verify_label)

        self.sections = {}
        for key, name, headers in (
            ("spec", "SPECIFICATION", ("Item", "Value", "Source")),
            ("steps", "STEPS", ("#", "Step", "")),
            (
                "results",
                "PLATEAU RESULTS",
                ("Cycle", "Plateau", "Target", "Dwell", "Range", "In band"),
            ),
            ("deviations", "DEVIATIONS", ("Time", "Where", "What", "Decision")),
            ("log", "SIGNED LOG", ("#", "Time", "Actor", "Action", "Detail")),
        ):
            label = QLabel(self.tr(name))
            label.setObjectName("sectionLabel")
            table = _table([self.tr(h) for h in headers])
            d.addWidget(label)
            d.addWidget(table)
            self.sections[key] = (label, table)
        d.addStretch(1)
        scroll.setWidget(detail)
        split.addWidget(scroll, 1)
        outer.addLayout(split, 1)
        self._show(None)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start(REFRESH_MS)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def select(self, campaign_id):
        self._selected = campaign_id
        self.refresh()

    def refresh(self):
        running = self._supervisor_running()
        self.supervisor_label.setText(
            self.tr("RUN SUPERVISOR ON")
            if running
            else self.tr("RUN SUPERVISOR OFF: TURN THE AGENT API ON (SETTINGS)")
        )
        try:
            campaigns = cs.list_campaigns(100)
        except Exception as exc:
            self.status_label.setText(str(exc))
            return
        listing = [(c["id"], c["title"], c["state"], c["spec"]["rehearsal"]) for c in campaigns]
        if listing == getattr(self, "_listing", None):
            self._show(cs.get_campaign(self._selected) if self._selected else None)
            return
        self._listing = listing
        self.list.blockSignals(True)
        self.list.clear()
        current = None
        for c in campaigns:
            item = QListWidgetItem(
                f"#{c['id']}  {c['title']}\n{self.tr(STATE_NAMES.get(c['state'], c['state']))}"
                + (self.tr(" · rehearsal") if c["spec"]["rehearsal"] else "")
            )
            item.setData(Qt.UserRole, c["id"])
            self.list.addItem(item)
            if c["id"] == self._selected:
                current = item
        if current is None and campaigns:
            current = self.list.item(0)
            self._selected = campaigns[0]["id"]
        if current is not None:
            self.list.setCurrentItem(current)
        self.list.blockSignals(False)
        self._show(cs.get_campaign(self._selected) if self._selected else None)

    def _on_select(self, item, _previous):
        if item is not None:
            self._selected = item.data(Qt.UserRole)
            self._show(cs.get_campaign(self._selected))

    def _show(self, c):
        has = c is not None
        for widget in (self.meta_label, self.status_label, *self.rail.values()):
            widget.setVisible(has)
        for label, table in self.sections.values():
            label.setVisible(has)
            table.setVisible(has)
        for button in (
            self.submit_btn,
            self.cancel_btn,
            self.stop_btn,
            self.report_btn,
            self.open_btn,
            self.sign_btn,
            self.verify_btn,
        ):
            button.setVisible(False)
        if not has:
            self.title_label.setText(self.tr("No campaign selected"))
            self.hold_card.setVisible(False)
            return
        spec, state = c["spec"], c["state"]
        self.title_label.setText(f"#{c['id']}  {c['title']}")
        self.meta_label.setText(
            f"{cs.spec_line(spec)} · {c['chamber_name']} · "
            + self.tr("about {0} h").format(f"{cs.estimate_hours(spec):g}")
            + f" · {self.tr('drafted by')} {c['created_by']}"
            + (f" · {self.tr('REHEARSAL')}" if spec["rehearsal"] else "")
            + f"\n{spec['pack']['name']}: {spec['pack']['note']}"
        )
        stage = cs.stage_of(c)
        order = list(STAGE_NAMES)
        for key, label in self.rail.items():
            if order.index(key) < order.index(stage):
                style = "color: #16a34a; font-weight: 600; border-bottom: 2px solid #16a34a;"
            elif key == stage:
                style = "color: #2563eb; font-weight: 800; border-bottom: 3px solid #2563eb;"
            else:
                style = "color: #94a3b8; border-bottom: 2px solid #cbd5e1;"
            label.setStyleSheet(style + " padding: 4px;")
        status = self.tr(STATE_NAMES.get(state, state))
        step = cs.step_progress(c)
        if step:
            status += f" · {self.tr('step')} {step['index']}/{step['of']}: {step['label']}"
            if step.get("status"):
                status += f" ({step['status']})"
        points = cs.trace(c["id"], (c["runtime"] or {}).get("last_trace_at", 0) - 1)
        if points and state in (cs.RUNNING, cs.HOLDING) and points[-1]["temp"] is not None:
            status += self.tr(" · chamber {0:.1f} °C, setpoint {1:.1f} °C").format(
                points[-1]["temp"], points[-1]["setpoint"] or 0.0
            )
        if c.get("end_reason"):
            status += f" · {c['end_reason']}"
        self.status_label.setText(status)
        self._show_hold(cs.open_hold_of(c["id"]) if state in cs.ACTIVE else None)

        self.submit_btn.setVisible(state == cs.DRAFT)
        self.cancel_btn.setVisible(state in (cs.DRAFT, cs.APPROVAL))
        self.stop_btn.setVisible(state in (cs.RUNNING, cs.HOLDING))
        self.report_btn.setVisible(state in cs.ENDED and not c.get("report_issued_at"))
        self.open_btn.setVisible(bool(c.get("report_files")))
        self.sign_btn.setVisible(bool(c.get("report_issued_at")) and not c.get("signed_off_by"))
        self.verify_btn.setVisible(True)

        _fill(self.sections["spec"][1], cs.spec_rows(spec))
        index = (c["runtime"] or {}).get("step_index", -1)
        _fill(
            self.sections["steps"][1],
            [
                (
                    i + 1,
                    s["label"],
                    "✓" if i < index or state in cs.ENDED else ("▶" if i == index else ""),
                )
                for i, s in enumerate(c["steps"])
            ],
        )
        _fill(
            self.sections["results"][1],
            [
                (
                    r["cycle"],
                    r["plateau"],
                    cs.deg(r["target_c"]),
                    f"{r['dwell_min']:g} min",
                    f"{r['min_c']:.1f} … {r['max_c']:.1f} °C",
                    self.tr("yes") if r["within_band"] else self.tr("no"),
                )
                for r in c["results"] or []
            ],
        )
        _fill(
            self.sections["deviations"][1],
            [
                (
                    campaign_report.local_time(d["time"]),
                    d["where"],
                    d["text"],
                    d.get("decision", ""),
                )
                for d in c["deviations"] or []
            ],
        )
        _fill(
            self.sections["log"][1],
            [
                (
                    e["seq"],
                    campaign_report.local_time(e["time"]),
                    e["actor"],
                    e["action"],
                    e["detail"],
                )
                for e in reversed(cs.log_entries(c["id"]))
            ],
        )

    def _show_hold(self, hold):
        self.hold_card.setVisible(hold is not None)
        if hold is None:
            self._shown_hold = None
            return
        if self._shown_hold == (hold["id"], (hold["recommendation"] or {}).get("at")):
            return
        self._shown_hold = (hold["id"], (hold["recommendation"] or {}).get("at"))
        self.hold_title.setText(hold["title"])
        self.hold_detail.setText(hold["detail"])
        rec = hold["recommendation"]
        labels = {o["key"]: o["label"] for o in hold["options"]}
        self.hold_recommendation.setVisible(bool(rec))
        if rec:
            self.hold_recommendation.setText(
                self.tr("{0} recommends: {1}. {2}").format(
                    rec["by"], labels.get(rec["option"], rec["option"]), rec["rationale"]
                )
            )
        while self.hold_options.count():
            item = self.hold_options.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for option in hold["options"]:
            text = option["label"] + "\n" + option["help"]
            if rec and rec["option"] == option["key"]:
                text = "★ " + text
            button = QPushButton(text)
            button.setObjectName("primaryButton" if rec and rec["option"] == option["key"] else "")
            button.setStyleSheet("text-align: left; padding: 8px 12px;")
            button.setMinimumHeight(button.fontMetrics().lineSpacing() * 2 + 20)
            button.clicked.connect(
                lambda _checked=False, h=hold["id"], k=option["key"]: self.decide(h, k)
            )
            self.hold_options.addWidget(button)

    def _warn(self, exc):
        QMessageBox.warning(self, self.tr("Test campaigns"), str(exc))

    def new_campaign(self):
        dialog = NewCampaignDialog(self)
        if dialog.chamber.count() == 0:
            self._warn(self.tr("Add a PLC chamber first: campaigns run on a PLC chamber."))
            return
        if dialog.exec() != QDialog.Accepted:
            return
        self.draft(dialog.requirement(), dialog.chamber.currentData())

    def draft(self, requirement, chamber):
        try:
            limits = control_settings.temperature_limits(control_settings.load())
            campaign = cs.draft(
                requirement,
                chamber,
                self.user_name or "operator",
                limits,
                actor=self.user_name or "Operator",
            )
        except (cs.CampaignError, KeyError, ValueError) as exc:
            self._warn(exc)
            return None
        self.select(campaign["id"])
        return campaign

    def submit(self):
        try:
            cs.submit(self._selected, actor=self.user_name or "Operator")
        except cs.CampaignError as exc:
            self._warn(exc)
        self.refresh()

    def cancel(self):
        try:
            cs.cancel(
                self._selected, self.user_name or "Operator", "cancelled in the Campaigns page"
            )
        except cs.CampaignError as exc:
            self._warn(exc)
        self.refresh()

    def decide(self, hold_id, choice):
        try:
            cs.decide_hold(hold_id, choice, self.user_name, self.hold_comment.text().strip())
        except cs.CampaignError as exc:
            self._warn(exc)
        self.hold_comment.clear()
        self._shown_hold = None
        self.refresh()

    def stop_now(self, confirm=True):
        if confirm:
            answer = QMessageBox.question(
                self,
                self.tr("Stop the campaign"),
                self.tr("Stop the chamber's job now and end the campaign? It can't be resumed."),
            )
            if answer not in (QMessageBox.Yes, QMessageBox.Ok):
                return
        try:
            cs.request_stop(self._selected, self.user_name, "stopped in the Campaigns page")
        except cs.CampaignError as exc:
            self._warn(exc)
        self.refresh()

    def issue_report(self):
        try:
            campaign_report.issue(self._selected, "", by=self.user_name or "Operator")
        except (cs.CampaignError, campaign_report.ReportError) as exc:
            self._warn(exc)
        self.refresh()

    def open_report(self):
        c = cs.get_campaign(self._selected)
        if c and c.get("report_files"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(c["report_files"]["pdf"]))

    def sign_off(self):
        try:
            campaign_report.sign_off(self._selected, self.user_name)
        except (cs.CampaignError, campaign_report.ReportError) as exc:
            self._warn(exc)
        self.refresh()

    def verify_log(self):
        check = cs.verify_chain(self._selected)
        if check["ok"]:
            self.verify_label.setText(
                self.tr("Log intact: {0} entries; last hash {1}").format(
                    check["entries"], check["last_hash"]
                )
            )
            self.verify_label.setStyleSheet("color: #16a34a;")
        else:
            self.verify_label.setText(
                self.tr(
                    "Log broken at entry {0}: an entry was changed after it was written."
                ).format(check["broken_at"])
            )
            self.verify_label.setStyleSheet("color: #dc2626; font-weight: 700;")
