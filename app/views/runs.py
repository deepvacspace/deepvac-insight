"""Run list and run opening."""

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

import app.common as common
import app.services.data_service as data
import app.services.settings_service as settings_service
from app.common import PAGE_ANALYSIS, _svg_icon
from app.run_tab import RunTabPage

RUN_DETAILS_ROLE = Qt.UserRole + 1


def _duration_text(seconds):
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" if secs < 30 or minutes >= 10 else f"{minutes} min {secs} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def safe_report_filename(run_id):
    safe = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in str(run_id)).strip()
    return safe or "run"


class RunListDelegate(QStyledItemDelegate):
    """Row delegate of the run list."""

    download_requested = Signal(str)
    ICON = 16
    PAD = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icons = {}

    def sizeHint(self, option, index):
        return QSize(0, 46)

    def icon_rect(self, rect):
        return QRect(
            rect.right() - self.PAD - self.ICON - 6,
            rect.center().y() - (self.ICON + 12) // 2,
            self.ICON + 12,
            self.ICON + 12,
        )

    def _icon(self, color):
        if color not in self._icons:
            self._icons[color] = _svg_icon("download", color, self.ICON).pixmap(
                self.ICON, self.ICON
            )
        return self._icons[color]

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        name = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, widget)
        icon_rect = self.icon_rect(option.rect)
        text_rect.setRight(icon_rect.left() - 6)
        selected = bool(option.state & QStyle.State_Selected)
        main = (
            option.palette.highlightedText().color() if selected else option.palette.text().color()
        )
        muted = QColor(main)
        muted.setAlpha(150)

        painter.save()
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(main)
        top = QRect(text_rect.left(), option.rect.top() + 5, text_rect.width(), 18)
        painter.drawText(
            top,
            Qt.AlignLeft | Qt.AlignVCenter,
            painter.fontMetrics().elidedText(name, Qt.ElideRight, top.width()),
        )
        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() - 1.0))
        painter.setFont(small)
        painter.setPen(muted)
        bottom = QRect(text_rect.left(), option.rect.top() + 24, text_rect.width(), 16)
        painter.drawText(bottom, Qt.AlignLeft | Qt.AlignVCenter, index.data(RUN_DETAILS_ROLE) or "")
        hovered = bool(option.state & QStyle.State_MouseOver)
        if hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(127, 127, 127, 50))
            painter.drawRoundedRect(icon_rect, 6, 6)
        pixmap = self._icon("#60a5fa" if hovered else "#94a3b8")
        painter.drawPixmap(
            icon_rect.center().x() - self.ICON // 2, icon_rect.center().y() - self.ICON // 2, pixmap
        )
        painter.restore()

    def editorEvent(self, event, model, option, index):
        mouse = (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick)
        if event.type() in mouse and self.icon_rect(option.rect).contains(
            event.position().toPoint()
        ):
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self.download_requested.emit(index.data(Qt.UserRole))
            return True
        return super().editorEvent(event, model, option, index)

    def helpEvent(self, event, view, option, index):
        if self.icon_rect(option.rect).contains(event.pos()):
            QToolTip.showText(event.globalPos(), self.tr("Download the run's report (.xlsx)"), view)
            return True
        return super().helpEvent(event, view, option, index)


class UploadWorker(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, paths):
        super().__init__()
        self.paths = paths

    def run(self):
        try:
            self.finished_ok.emit(data.upload_runs(self.paths))
        except Exception as exc:
            self.failed.emit(str(exc))


class RunsMixin:
    def _build_runs_drawer(self):
        """Builds the run list drawer."""
        from app.overlay_drawer import OverlayDrawer

        self._runs_drawer_target = None
        self._run_click_consumed = False
        self.runs_drawer = OverlayDrawer(self.editor_area, 340)
        self.runs_drawer.set_content(self._runs_browser())
        self.editor_area.add_requested.connect(self._open_runs_drawer)

    def _runs_browser(self):
        panel = QWidget()
        panel.setObjectName("runsPanel")
        panel.setAttribute(Qt.WA_StyledBackground)
        left_lay = QVBoxLayout(panel)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)

        header = QFrame()
        header.setObjectName("runsPanelHeader")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 6, 8, 6)
        h_lay.setSpacing(4)
        lbl = QLabel(self.tr("OPEN A RUN"))
        lbl.setObjectName("sidebarPanelLabel")
        h_lay.addWidget(lbl, 1)

        self.upload_btn = QPushButton()
        self.upload_btn.setObjectName("runsUploadButton")
        self.upload_btn.setIcon(_svg_icon("database", "#94a3b8", 14))
        self.upload_btn.setFixedSize(24, 24)
        self.upload_btn.setToolTip(self.tr("Upload run(s) into the database"))
        self.upload_btn.clicked.connect(self._show_upload_menu)
        h_lay.addWidget(self.upload_btn)
        close_btn = QPushButton("×")
        close_btn.setObjectName("runsUploadButton")
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip(self.tr("Close (Esc)"))
        close_btn.clicked.connect(lambda: self.runs_drawer.close())
        h_lay.addWidget(close_btn)
        left_lay.addWidget(header)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText(self.tr("Search run id…"))
        self.search_box.addAction(_svg_icon("search", "#64748b", 13), QLineEdit.LeadingPosition)
        self.search_box.textChanged.connect(self.render_runs)
        self.search_box.returnPressed.connect(self._open_first_listed_run)
        left_lay.addWidget(self.search_box)

        self.run_list = QListWidget()
        self.run_list.setObjectName("runList")
        self.run_list.setUniformItemSizes(True)
        self.run_list.setMouseTracking(True)
        self.run_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._run_list_delegate = RunListDelegate(self.run_list)
        self._run_list_delegate.download_requested.connect(self._download_run_report)
        self.run_list.setItemDelegate(self._run_list_delegate)
        self.run_list.itemChanged.connect(self._run_checked)
        self.run_list.itemClicked.connect(self._on_run_clicked)
        self.run_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.run_list.customContextMenuRequested.connect(self._run_list_context_menu)
        left_lay.addWidget(self.run_list, 1)

        hint = QLabel(
            self.tr("Click a run to open it. Tick runs to compare them with the active tab.")
        )
        hint.setObjectName("runsDrawerHint")
        hint.setWordWrap(True)
        left_lay.addWidget(hint)
        return panel

    def _open_runs_drawer(self, group=None):
        """Opens the run list over Analysis."""
        self._runs_drawer_target = group
        self._nav_to(PAGE_ANALYSIS)
        self.render_runs()
        self.runs_drawer.open()
        self.search_box.setFocus()
        self.search_box.selectAll()

    def load_runs(self):
        from PySide6.QtWidgets import QMessageBox

        try:
            self.splash_msg(self.tr("Loading runs…"))

            def progress(i, total, msg):
                self.splash_msg(self.tr("Loading runs…"))

            payload = data.list_runs(progress=progress)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Unable to load runs"), str(exc))
            return
        self.runs = payload["runs"]
        self.render_runs()
        self._restore_open_tabs()

    def _restore_open_tabs(self):
        available = {r["key"] for r in self.runs}
        keys = [k for k in settings_service.load_open_tabs() if k in available]
        if not keys and self.runs:
            keys = [self.runs[0]["key"]]
        for key in keys:
            self._open_run(key)
        active = settings_service.load_active_tab()
        if active in keys:
            self._open_run(active)

    def render_runs(self):
        query = self.search_box.text().lower().strip()
        active_page = self.editor_area.active_page()
        compare_keys = active_page.compare_runs if active_page else set()
        self.run_list.blockSignals(True)
        self.run_list.clear()
        for run in self.runs:
            haystack = " ".join(str(v) for v in run.values()).lower()
            if query and query not in haystack:
                continue
            item = QListWidgetItem(run["id"])
            item.setData(Qt.UserRole, run["key"])
            samples = run.get("samples")
            item.setData(
                RUN_DETAILS_ROLE,
                self.tr("{0} · {1} samples").format(
                    _duration_text(run.get("duration_s")), samples if samples is not None else "—"
                ),
            )
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if run["key"] in compare_keys else Qt.Unchecked)
            self.run_list.addItem(item)
        self.run_list.blockSignals(False)

    def _download_run_report(self, key):
        self._run_click_consumed = True
        self._write_run_report(key)

    def _write_run_report(self, key):
        """Writes a run's report to a chosen file."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        run = next((r for r in self.runs if r["key"] == key), None)
        if not run:
            return
        reports_dir = common.REPORTS_DIR
        reports_dir.mkdir(parents=True, exist_ok=True)
        default = reports_dir / f"{safe_report_filename(run['id'])}.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Download report"), str(default), self.tr("Excel workbook (*.xlsx)")
        )
        if not path:
            return
        try:
            data.make_report_xlsx(key, path)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Report error"), str(exc))
            return

    def _open_run_item(self, item):
        self._open_run(item.data(Qt.UserRole), self._runs_drawer_target)
        self.runs_drawer.close()
        self._nav_to(PAGE_ANALYSIS)

    def _on_run_clicked(self, item):
        if self._run_click_consumed:
            self._run_click_consumed = False
            return
        self._open_run_item(item)

    def _open_first_listed_run(self):
        if self.run_list.count():
            self._open_run_item(self.run_list.item(0))

    def _run_list_context_menu(self, pos):
        item = self.run_list.itemAt(pos)
        if not item:
            return
        key = item.data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction(self.tr("Open"))
        act_side = menu.addAction(self.tr("Open to the Side"))
        menu.addSeparator()
        act_rename = menu.addAction(self.tr("Rename…"))
        act_report = menu.addAction(self.tr("Download Report (.xlsx)…"))
        chosen = menu.exec(self.run_list.viewport().mapToGlobal(pos))
        self._run_click_consumed = False
        if chosen == act_open:
            self._open_run_item(item)
        elif chosen == act_side:
            self.runs_drawer.close()
            self._open_run(key)
            self.editor_area.split_page_right(key)
            self._nav_to(PAGE_ANALYSIS)
        elif chosen == act_rename:
            self._rename_run(key)
        elif chosen == act_report:
            self._write_run_report(key)

    def _rename_run(self, key):
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        run = next((r for r in self.runs if r["key"] == key), None)
        if not run:
            return
        new_name, ok = QInputDialog.getText(
            self, self.tr("Rename run"), self.tr("Name:"), text=run["id"]
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == run["id"]:
            return
        try:
            data.rename_run(key, new_name)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Rename failed"), str(exc))
            return
        run["id"] = new_name
        self.render_runs()
        self._refresh_reports()
        self.editor_area.rename_open_run(key, new_name)

    def _show_upload_menu(self):
        menu = QMenu(self)
        act_folders = menu.addAction(self.tr("Upload Folder(s)…"))
        act_files = menu.addAction(self.tr("Upload File(s)…"))
        chosen = menu.exec(self.upload_btn.mapToGlobal(self.upload_btn.rect().bottomLeft()))
        if chosen == act_folders:
            self._upload_folders()
        elif chosen == act_files:
            self._upload_files()

    def _pick_multiple_dirs(self, title):
        from PySide6.QtWidgets import (
            QAbstractItemView,
            QFileDialog,
            QListView,
            QTreeView,
        )

        dialog = QFileDialog(self, title)
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        for view in dialog.findChildren((QListView, QTreeView)):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if dialog.exec() != QFileDialog.Accepted:
            return []
        return dialog.selectedFiles()

    def _upload_folders(self):
        dirs = self._pick_multiple_dirs(
            self.tr(
                "Select run folder(s) to upload (a folder may hold one run or many run subfolders)"
            )
        )
        if dirs:
            self._start_upload(dirs)

    def _upload_files(self):
        from PySide6.QtWidgets import QFileDialog

        files, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("Select run_samples.csv file(s)"),
            "",
            self.tr("Run samples (run_samples.csv);;CSV files (*.csv);;All files (*)"),
        )
        if files:
            self._start_upload(files)

    def _start_upload(self, paths):
        self.upload_btn.setEnabled(False)
        self.upload_btn.setToolTip(self.tr("Uploading…"))
        self._upload_worker = UploadWorker(paths)
        self._upload_worker.finished_ok.connect(self._upload_done)
        self._upload_worker.failed.connect(self._upload_failed)
        self._upload_worker.start()

    def _upload_done(self, result):
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QMessageBox

        self.upload_btn.setEnabled(True)
        self.upload_btn.setToolTip(self.tr("Upload run(s) into the database"))
        self.runs = result["runs"]
        self.render_runs()
        self._refresh_reports()
        n = len(result["imported"])
        message = QCoreApplication.translate(
            "RunsMixin", "Imported %n run(s) into the database.", "", n
        )
        QMessageBox.information(self, self.tr("Upload complete"), message)

    def _upload_failed(self, msg):
        from PySide6.QtWidgets import QMessageBox

        self.upload_btn.setEnabled(True)
        self.upload_btn.setToolTip(self.tr("Upload run(s) into the database"))
        QMessageBox.critical(self, self.tr("Upload failed"), msg)

    def _open_run(self, key, group=None):
        run = next((r for r in self.runs if r["key"] == key), None)
        if not run:
            return
        run_id = run["id"]
        open_page = self.editor_area.page_for(key)
        if open_page is not None:
            self.editor_area.open_run(key, run_id, open_page)
            return
        page = RunTabPage(key, self.runs, dark=self.dark, current_user=self.current_user)
        page.compare_changed.connect(lambda keys: self.render_runs())
        self.editor_area.register_chart(page.chart)
        self.editor_area.open_run(key, run_id, page, group)
        page.load()

    def _run_checked(self, item):
        self._run_click_consumed = True
        key = item.data(Qt.UserRole)
        checked = item.checkState() == Qt.Checked
        page = self.editor_area.active_page()
        if page:
            page.set_compare_run(key, checked)

    def _on_active_page_changed(self, page):
        self.render_runs()
