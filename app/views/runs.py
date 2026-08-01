"""RunsMixin — builds the Runs browser page and manages run opening/comparison."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

import app.services.data_service as data
import app.services.settings_service as settings_service
from app.common import _svg_icon, fmt
from app.run_tab import RunTabPage


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
    def _runs_view(self):
        container = QWidget()
        container.setObjectName("workspaceBody")
        lay = QHBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        left = QFrame()
        left.setObjectName("runsPanel")
        left.setFixedWidth(280)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)

        header = QFrame()
        header.setObjectName("runsPanelHeader")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 10, 8, 10)
        h_lay.setSpacing(0)
        lbl = QLabel(self.tr("RUNS"))
        lbl.setObjectName("sidebarPanelLabel")
        h_lay.addWidget(lbl, 1)

        self.upload_btn = QPushButton()
        self.upload_btn.setObjectName("runsUploadButton")
        self.upload_btn.setIcon(_svg_icon("database", "#94a3b8", 14))
        self.upload_btn.setFixedSize(24, 24)
        self.upload_btn.setToolTip(self.tr("Upload run(s) into the database"))
        self.upload_btn.clicked.connect(self._show_upload_menu)
        h_lay.addWidget(self.upload_btn)
        left_lay.addWidget(header)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText(self.tr("Search run id…"))
        self.search_box.addAction(_svg_icon("search", "#64748b", 13), QLineEdit.LeadingPosition)
        self.search_box.textChanged.connect(self.render_runs)
        left_lay.addWidget(self.search_box)

        self.run_list = QListWidget()
        self.run_list.setObjectName("runList")
        self.run_list.setUniformItemSizes(True)
        self.run_list.itemChanged.connect(self._run_checked)
        self.run_list.itemDoubleClicked.connect(self._open_run_item)
        self.run_list.currentItemChanged.connect(self._on_run_selected)
        self.run_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.run_list.customContextMenuRequested.connect(self._run_list_context_menu)
        left_lay.addWidget(self.run_list, 1)
        lay.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(16, 16, 16, 16)
        right_lay.setSpacing(8)
        self._raw_run_label = QLabel(self.tr("Select a run to view raw data"))
        self._raw_run_label.setObjectName("title")
        right_lay.addWidget(self._raw_run_label)
        self._raw_run_table = QTableWidget()
        self._raw_run_table.setMinimumHeight(400)
        right_lay.addWidget(self._raw_run_table)
        lay.addWidget(right, 1)

        return container

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
        self._refresh_dashboard()
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
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if run["key"] in compare_keys else Qt.Unchecked)
            self.run_list.addItem(item)
        self.run_list.blockSignals(False)

    def _open_run_item(self, item):
        self._open_run(item.data(Qt.UserRole))
        self._nav_to(2)

    def _run_list_context_menu(self, pos):
        item = self.run_list.itemAt(pos)
        if not item:
            return
        key = item.data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction(self.tr("Open in Analysis"))
        act_rename = menu.addAction(self.tr("Rename…"))
        chosen = menu.exec(self.run_list.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._open_run(key)
            self._nav_to(2)
        elif chosen == act_rename:
            self._rename_run(key)

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
        self._refresh_dashboard()
        self._refresh_reports()
        self.editor_area.rename_open_run(key, new_name)
        current = self.run_list.currentItem()
        if current and current.data(Qt.UserRole) == key:
            self._raw_run_label.setText(new_name)

    # ── Upload ───────────────────────────────────────────────────────────────

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
        self._refresh_dashboard()
        self._refresh_reports()
        n = len(result["imported"])
        # self.tr()'s implicit context resolution doesn't reliably reach the
        # %n/plural overload here (self is a DeepVacDesktop instance, not a
        # RunsMixin one) -- call QCoreApplication.translate() directly with
        # the exact context pyside6-lupdate recorded for this string.
        message = QCoreApplication.translate(
            "RunsMixin", "Imported %n run(s) into the database.", "", n
        )
        QMessageBox.information(self, self.tr("Upload complete"), message)

    def _upload_failed(self, msg):
        from PySide6.QtWidgets import QMessageBox

        self.upload_btn.setEnabled(True)
        self.upload_btn.setToolTip(self.tr("Upload run(s) into the database"))
        QMessageBox.critical(self, self.tr("Upload failed"), msg)

    def _open_run(self, key):
        run = next((r for r in self.runs if r["key"] == key), None)
        if not run:
            return
        run_id = run["id"]
        for grp in self.editor_area.all_groups():
            if grp.has_key(key):
                grp.tab_bar.add_or_focus(key, run_id)
                return
        page = RunTabPage(key, self.runs, dark=self.dark, current_user=self.current_user)
        page.compare_changed.connect(lambda keys: self.render_runs())
        self.editor_area.register_chart(page.chart)
        self.editor_area.open_run(key, run_id, page)
        page.load()

    def _run_checked(self, item):
        key = item.data(Qt.UserRole)
        checked = item.checkState() == Qt.Checked
        page = self.editor_area.active_page()
        if page:
            page.set_compare_run(key, checked)

    def _on_active_page_changed(self, page):
        self.render_runs()

    def _on_run_selected(self, item, _prev):
        if not item:
            return
        key = item.data(Qt.UserRole)
        run = next((r for r in self.runs if r["key"] == key), None)
        if not run:
            return
        self._raw_run_label.setText(run["id"])
        try:
            table = data.run_table(key)
            self._fill_generic_table(
                self._raw_run_table, table["columns"], table["rows"], max_rows=2000
            )
        except Exception:
            pass

    def _fill_generic_table(self, table, columns, rows, max_rows=None):
        from PySide6.QtWidgets import QTableWidgetItem

        shown = rows[:max_rows] if max_rows else rows
        table.setUpdatesEnabled(False)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnCount(len(columns))
        table.setRowCount(len(shown))
        table.setHorizontalHeaderLabels(columns)
        for ri, row in enumerate(shown):
            for ci, col in enumerate(columns):
                table.setItem(ri, ci, QTableWidgetItem(fmt(row.get(col))))
        table.resizeColumnsToContents()
        table.resizeRowsToContents()
        table.setUpdatesEnabled(True)
