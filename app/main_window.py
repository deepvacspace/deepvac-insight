"""DeepVacDesktop — the main application window."""

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QSizeGrip,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.common import ICON_PATH, _nav_icon, _svg_icon
from app.monitor_tab import MonitorTabPage
from app.services import i18n_service, settings_service
from app.services.chamber_session import MAX_CONNECTED_CHAMBERS, ChamberSession
from app.services.opc_broadcast_server import OpcBroadcastServer
from app.tab_system import EditorArea
from app.title_bar import TitleBar
from app.views.controller import ControllerMixin
from app.views.dashboard import DashboardMixin
from app.views.monitoring import MonitoringMixin
from app.views.opc import OpcMixin
from app.views.reports import ReportsMixin
from app.views.runs import RunsMixin
from app.views.simulator import SimulatorMixin


class DeepVacDesktop(
    DashboardMixin,
    RunsMixin,
    SimulatorMixin,
    ReportsMixin,
    MonitoringMixin,
    ControllerMixin,
    OpcMixin,
    QMainWindow,
):
    def __init__(self, splash=None, current_user=None):
        super().__init__()
        self.splash = splash
        self.current_user = current_user or {"id": None, "name": "User", "email": ""}
        self.logout_requested = False
        self.setWindowTitle(self.tr("DeepVac Dashboard"))
        self.setWindowIcon(QIcon(ICON_PATH))
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(1500, 920)
        self.setMinimumSize(1100, 700)

        self.runs = []
        self.dark = settings_service.load_theme()
        self.sim_worker = None
        self.sim_series = None
        self._sim_anim_timer = None
        self._sim_anim_data = []
        self._sim_anim_curves = []
        self._sim_anim_idx = 0
        self._sim_anim_total = 0

        self._chamber_sessions = {}  # chamber id -> ChamberSession, see _connect_chamber()
        self.opc_server = OpcBroadcastServer(self)

        self._build_ui()

        self.apply_theme()
        self.load_runs()

        # A daily backup already runs at process startup (app.app.main); this
        # periodic check covers sessions left open across a day boundary.
        # backup_all() is a same-day no-op, so firing this often is cheap.
        self._backup_timer = QTimer(self)
        self._backup_timer.timeout.connect(self._run_background_backup)
        self._backup_timer.start(6 * 60 * 60 * 1000)

    def _run_background_backup(self):
        from app.services import backup_service

        try:
            backup_service.backup_all()
        except Exception as exc:
            print(f"[backup] periodic backup skipped: {exc}")

    # ── Chamber connections (shared by Live Monitoring, Controller, OPC Server) ─
    #
    # Up to MAX_CONNECTED_CHAMBERS ChamberSessions can be open at once, each
    # owning one tab in self.monitor_editor_area (see views/monitoring.py).
    # This is the one place that creates/destroys them so Controller
    # (views/controller.py) and the OPC broadcast server can both reach
    # whichever chambers are currently connected without owning the
    # lifecycle themselves.

    def _connect_chamber(self, chamber):
        if chamber["id"] in self._chamber_sessions:
            raise RuntimeError(self.tr("Already connected to this chamber."))
        if len(self._chamber_sessions) >= MAX_CONNECTED_CHAMBERS:
            raise RuntimeError(
                self.tr("Up to {0} chambers can be connected at once.").format(
                    MAX_CONNECTED_CHAMBERS
                )
            )

        session = ChamberSession(chamber, self)
        self._chamber_sessions[chamber["id"]] = session
        session.connected.connect(self._recompute_chamber_aggregate_status)
        session.disconnected.connect(self._recompute_chamber_aggregate_status)
        session.sample_received.connect(
            lambda sample, s=session: self.opc_server.broadcast(s.chamber["name"], sample)
        )
        session.manual_state_changed.connect(
            lambda s=session: self._on_controller_session_changed(s)
        )
        session.test_state_changed.connect(lambda s=session: self._on_controller_session_changed(s))

        page = MonitorTabPage(session, current_user=self.current_user)
        self.monitor_editor_area.open_run(f"chamber-{chamber['id']}", chamber["name"], page)

        session.connect()
        self._refresh_chamber_ui()
        return session

    def _disconnect_chamber(self, chamber_id):
        session = self._chamber_sessions.pop(chamber_id, None)
        if session is None:
            return
        session.teardown()
        session.deleteLater()
        self._refresh_chamber_ui()

    def _connected_chamber_count(self):
        return sum(1 for s in self._chamber_sessions.values() if s.is_connected())

    def _any_chamber_connected(self):
        return self._connected_chamber_count() > 0

    def _refresh_chamber_ui(self):
        """The one place that keeps every chamber-count-dependent widget in
        sync after a session is added or removed -- called by both
        _connect_chamber() and _disconnect_chamber() so callers (the
        Monitoring page's Connect button, a tab's close button, tests) never
        need to remember to refresh each page themselves."""
        self._load_chamber_choices()
        self._update_mon_count_label()
        self._refresh_controller_chamber_choices()
        self._recompute_chamber_aggregate_status()

    def _recompute_chamber_aggregate_status(self):
        count = self._connected_chamber_count()
        self.title_bar.set_chamber_status(count, MAX_CONNECTED_CHAMBERS)
        self.title_bar.set_bell_active(count > 0)
        self._opc_set_chamber_connected(count > 0)

    # ── Persisted UI state ───────────────────────────────────────────────────

    def restore_window_state(self):
        geo = settings_service.load_window_geometry()
        if geo is not None:
            self.restoreGeometry(geo)
        if settings_service.load_window_maximized():
            self.showMaximized()
        else:
            self.show()

    def closeEvent(self, event):
        self._persist_ui_state()
        if self.opc_server.is_running():
            self.opc_server.stop()
        for session in list(self._chamber_sessions.values()):
            session.teardown()
        super().closeEvent(event)

    def _persist_ui_state(self):
        settings_service.save_theme(self.dark)
        settings_service.save_window_geometry(self.saveGeometry())
        settings_service.save_window_maximized(self.isMaximized())
        open_keys = []
        for grp in self.editor_area.all_groups():
            open_keys.extend(grp.tab_bar.all_keys())
        settings_service.save_open_tabs(open_keys)
        active_page = self.editor_area.active_page()
        settings_service.save_active_tab(active_page.run_key if active_page else None)

    # ── UI skeleton ───────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)

        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        body_lay.addWidget(self._sidebar())

        self.content_stack = QStackedWidget()
        self.dashboard_widget = self._dashboard_view()  # 0
        self.runs_widget = self._runs_view()  # 1
        self.editor_area = EditorArea()  # 2
        self.editor_area.active_page_changed.connect(self._on_active_page_changed)
        self.sim_widget = self._sim_view()  # 3
        self.reports_widget = self._reports_view()  # 4
        self.monitor_widget = self._monitoring_view()  # 5
        self.controller_widget = self._controller_view()  # 6
        self.opc_widget = self._opc_view()  # 7
        for w in [
            self.dashboard_widget,
            self.runs_widget,
            self.editor_area,
            self.sim_widget,
            self.reports_widget,
            self.monitor_widget,
            self.controller_widget,
            self.opc_widget,
        ]:
            self.content_stack.addWidget(w)

        body_lay.addWidget(self.content_stack, 1)
        outer.addWidget(body, 1)

        grip = QSizeGrip(central)
        grip.setFixedSize(14, 14)
        grip_lay = QHBoxLayout()
        grip_lay.setContentsMargins(0, 0, 0, 0)
        grip_lay.addStretch(1)
        grip_lay.addWidget(grip)
        outer.addLayout(grip_lay)

        self.setCentralWidget(central)
        self._nav_to(0)

    def _sidebar(self):
        from PySide6.QtWidgets import QFrame, QPushButton, QVBoxLayout

        bar = QFrame()
        bar.setObjectName("activityBar")
        bar.setFixedWidth(48)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(2)

        nav_defs = [
            ("act_dashboard_btn", "house", self.tr("Dashboard"), 0),
            ("act_runs_btn", "database", self.tr("Runs"), 1),
            ("act_analysis_btn", "activity", self.tr("Analysis"), 2),
            ("act_sim_btn", "cpu", self.tr("Simulator"), 3),
            ("act_reports_btn", "file-earmark", self.tr("Reports"), 4),
            ("act_monitor_btn", "graph-up", self.tr("Monitor"), 5),
            ("act_controller_btn", "square", self.tr("Controller"), 6),
            ("act_opc_btn", "broadcast", self.tr("OPC Server"), 7),
        ]
        self._nav_buttons = []
        for attr, icon_name, label, idx in nav_defs:
            btn = QPushButton()
            btn.setObjectName("navButton")
            btn.setCheckable(True)
            btn.setFixedSize(48, 46)
            btn.setToolTip(label)
            btn.setIconSize(QSize(22, 22))
            btn.setProperty("nav_icon", icon_name)
            btn.clicked.connect(lambda _checked, i=idx: self._nav_to(i))
            setattr(self, attr, btn)
            self._nav_buttons.append(btn)
            lay.addWidget(btn)

        lay.addStretch(1)

        self.act_account_btn = QPushButton()
        self.act_account_btn.setObjectName("navButton")
        self.act_account_btn.setFixedSize(48, 46)
        self.act_account_btn.setToolTip(self.current_user.get("name") or self.tr("Account"))
        self.act_account_btn.setIconSize(QSize(22, 22))
        self.act_account_btn.clicked.connect(self._show_account_menu)
        lay.addWidget(self.act_account_btn)

        self.act_settings_btn = QPushButton()
        self.act_settings_btn.setObjectName("navButton")
        self.act_settings_btn.setFixedSize(48, 46)
        self.act_settings_btn.setToolTip(self.tr("Settings"))
        self.act_settings_btn.setIconSize(QSize(20, 20))
        self.act_settings_btn.clicked.connect(self._show_settings)
        lay.addWidget(self.act_settings_btn)

        return bar

    def _rebuild_nav_icons(self, c: dict):
        for btn in self._nav_buttons:
            name = btn.property("nav_icon")
            if name:
                btn.setIcon(_nav_icon(name, c["muted"], c["accent"]))
        self.act_account_btn.setIcon(_svg_icon("person-circle", c["muted"], 22))
        self.act_settings_btn.setIcon(_svg_icon("gear", c["muted"], 20))
        self.title_bar.btn_bell.setIcon(_svg_icon("bell", c["muted"], 16))

    def _nav_to(self, index):
        for i, btn in enumerate(self._nav_buttons):
            btn.setChecked(i == index)
        self.content_stack.setCurrentIndex(index)
        if index == 0:
            self._refresh_dashboard()
        elif index == 4:
            self._refresh_reports()
        elif index == 6:
            self._load_test_profiles()

    # ── Settings ──────────────────────────────────────────────────────────────

    def _show_settings(self):
        menu = QMenu(self)
        themes = menu.addMenu(self.tr("Themes"))
        act_dark = themes.addAction(self.tr("Dark"))
        act_dark.setCheckable(True)
        act_dark.setChecked(self.dark)
        act_light = themes.addAction(self.tr("Light"))
        act_light.setCheckable(True)
        act_light.setChecked(not self.dark)

        languages = menu.addMenu(self.tr("Language"))
        current_lang = settings_service.load_language()
        lang_actions = {}
        for code, name in i18n_service.AVAILABLE_LANGUAGES.items():
            act = languages.addAction(name)
            act.setCheckable(True)
            act.setChecked(code == current_lang)
            lang_actions[act] = code

        menu.addSeparator()
        act_backup_now = menu.addAction(self.tr("Back Up Now"))
        act_open_backups = menu.addAction(self.tr("Open Backups Folder"))
        btn = self.act_settings_btn
        chosen = menu.exec(btn.mapToGlobal(QPoint(btn.width() + 4, 0)))
        if chosen == act_dark and not self.dark:
            self.dark = True
            self.apply_theme()
        elif chosen == act_light and self.dark:
            self.dark = False
            self.apply_theme()
        elif chosen == act_backup_now:
            self._backup_now()
        elif chosen == act_open_backups:
            self._open_backups_folder()
        elif chosen in lang_actions:
            self._change_language(lang_actions[chosen])

    def toggle_theme(self):
        self.dark = not self.dark
        self.apply_theme()

    def _change_language(self, code):
        from PySide6.QtWidgets import QMessageBox

        if code == settings_service.load_language():
            return
        settings_service.save_language(code)
        QMessageBox.information(
            self,
            self.tr("Language"),
            self.tr("The new language will take effect the next time you start DeepVac."),
        )

    def _backup_now(self):
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QMessageBox

        from app.services import backup_service

        try:
            results = backup_service.backup_all(force=True)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Backup"), self.tr("Backup failed: {0}").format(exc))
            return
        # self.tr()'s %n/plural overload doesn't reliably resolve context in
        # this PySide6 version -- call QCoreApplication.translate() directly
        # with the exact context pyside6-lupdate recorded for this string.
        QMessageBox.information(
            self,
            self.tr("Backup"),
            QCoreApplication.translate(
                "DeepVacDesktop", "Backed up %n database(s) to data/backups/.", "", len(results)
            ),
        )

    def _open_backups_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from app.services.backup_service import BACKUPS_DIR

        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(BACKUPS_DIR)))

    # ── Account ───────────────────────────────────────────────────────────────

    def _show_account_menu(self):
        menu = QMenu(self)
        header = menu.addAction(self.current_user.get("name") or self.tr("Account"))
        header.setEnabled(False)
        menu.addSeparator()
        act_profile = menu.addAction(self.tr("Profile"))
        act_logout = menu.addAction(self.tr("Log out"))
        btn = self.act_account_btn
        chosen = menu.exec(btn.mapToGlobal(QPoint(btn.width() + 4, 0)))
        if chosen == act_profile:
            self._show_profile_dialog()
        elif chosen == act_logout:
            self._logout()

    def _show_profile_dialog(self):
        from app.profile_dialog import ProfileDialog

        dlg = ProfileDialog(self.current_user, self)
        dlg.exec()
        self.current_user = dlg.updated_user
        self.act_account_btn.setToolTip(self.current_user.get("name") or self.tr("Account"))

    def _logout(self):
        from PySide6.QtCore import QSettings

        from app.services import auth_service

        settings = QSettings("DeepVac", "Insight")
        token = settings.value("auth/remember_token", "")
        if token:
            auth_service.clear_remember_token(token)
            settings.remove("auth/remember_token")
        self.logout_requested = True
        self.close()

    # ── Utilities ─────────────────────────────────────────────────────────────

    def splash_msg(self, msg):
        from PySide6.QtWidgets import QApplication

        if self.splash:
            self.splash.showMessage(msg, Qt.AlignCenter | Qt.AlignBottom, Qt.white)
            QApplication.processEvents()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    # ── Theme ─────────────────────────────────────────────────────────────────

    def apply_theme(self):
        if self.dark:
            c = {
                "bg": "#0b1020",
                "panel": "#111827",
                "panel2": "#0f172a",
                "panel3": "#1e293b",
                "text": "#f8fafc",
                "muted": "#94a3b8",
                "border": "#253247",
                "border2": "#1f2937",
                "accent": "#60a5fa",
                "accent2": "#2563eb",
                "atext": "#ffffff",
                "hover": "#172033",
                "sel": "#1d4ed8",
                "talt": "#0d1526",
                "sbg": "#0b1020",
                "sh": "#334155",
                "tab_bg": "#1e2433",
                "tab_active": "#111827",
                "tab_border": "#60a5fa",
                "tab_text": "#94a3b8",
                "tab_active_text": "#f8fafc",
            }
        else:
            c = {
                "bg": "#eef2f7",
                "panel": "#ffffff",
                "panel2": "#f8fafc",
                "panel3": "#eef2ff",
                "text": "#0f172a",
                "muted": "#64748b",
                "border": "#d8e0ea",
                "border2": "#e5eaf2",
                "accent": "#2563eb",
                "accent2": "#1d4ed8",
                "atext": "#ffffff",
                "hover": "#f1f5f9",
                "sel": "#dbeafe",
                "talt": "#f8fafc",
                "sbg": "#eef2f7",
                "sh": "#cbd5e1",
                "tab_bg": "#ececec",
                "tab_active": "#ffffff",
                "tab_border": "#2563eb",
                "tab_text": "#64748b",
                "tab_active_text": "#0f172a",
            }

        css = f"""
            QWidget {{
                background: {c["bg"]};
                color: {c["text"]};
                font-family: "Segoe UI", "Inter", "Arial";
                font-size: 10.5pt;
            }}
            QWidget#titleBar {{
                background: {c["panel2"]};
                border-bottom: 2px solid {c["accent"]};
                min-height: 40px; max-height: 40px;
            }}
            QWidget#titleBarLogoArea {{
                background: transparent;
            }}
            QLabel#titleBarBrand {{
                font-size: 11px; font-weight: 900;
                color: {c["accent"]};
                background: transparent;
            }}
            QPushButton#titleCenter {{
                background: {c["panel3"]};
                border: 1px solid {c["border"]};
                border-radius: 4px;
                color: {c["muted"]};
                font-size: 10pt;
                padding: 0 10px;
                text-align: center;
            }}
            QPushButton#titleCenter:hover {{
                border-color: {c["accent"]};
                color: {c["text"]};
                background: {c["hover"]};
            }}
            QPushButton#winBtn {{
                background: transparent;
                border: none;
                border-radius: 0;
                color: {c["muted"]};
                font-size: 11px;
                padding: 0;
            }}
            QPushButton#winBtn:hover {{
                background: {c["hover"]};
                color: {c["text"]};
            }}
            QPushButton#winBtnClose {{
                background: transparent;
                border: none;
                border-radius: 0;
                color: {c["muted"]};
                font-size: 11px;
                padding: 0;
            }}
            QPushButton#winBtnClose:hover {{
                background: #c42b1c;
                color: #ffffff;
            }}
            QFrame#titleStatusPill {{
                background: transparent;
                border: none;
                min-height: 28px; max-height: 28px;
            }}
            QLabel#sysDotOk {{
                color: #22c55e; font-size: 9px; background: transparent;
            }}
            QLabel#sysDotErr {{
                color: #ef4444; font-size: 9px; background: transparent;
            }}
            QLabel#chamberIconOn {{
                color: #22c55e; font-size: 11px; background: transparent;
            }}
            QLabel#chamberIconOff {{
                color: #ef4444; font-size: 11px; background: transparent;
            }}
            QLabel#statusText {{
                font-size: 9.5pt; color: {c["muted"]}; background: transparent;
            }}
            QPushButton#titleIconBtn {{
                background: transparent; border: none;
                border-radius: 4px; font-size: 14px; padding: 0;
            }}
            QPushButton#titleIconBtn:hover {{ background: {c["hover"]}; }}
            QFrame#activityBar {{
                background: {c["panel2"]};
                border-right: 1px solid {c["border"]};
                min-width: 48px; max-width: 48px;
            }}
            QPushButton#navButton {{
                background: transparent;
                border: none;
                border-left: 3px solid transparent;
                color: {c["muted"]};
                font-size: 20px; padding: 0; border-radius: 0; text-align: center;
            }}
            QPushButton#navButton:hover {{
                color: {c["text"]}; background: {c["hover"]};
            }}
            QPushButton#navButton:checked {{
                color: {c["accent"]};
                border-left-color: {c["accent"]};
                background: {c["hover"]};
            }}
            QLabel#pageTitle {{
                font-size: 22px; font-weight: 800;
                color: {c["text"]}; background: transparent;
            }}
            QFrame#runsPanel {{
                background: {c["panel"]};
                border-right: 1px solid {c["border"]};
            }}
            QFrame#runsPanelHeader {{
                background: {c["panel"]};
                border-bottom: 1px solid {c["border2"]};
                min-height: 36px; max-height: 36px;
            }}
            QLabel#sidebarPanelLabel {{
                font-size: 10px; font-weight: 800;
                color: {c["muted"]}; letter-spacing: 1.2px; background: transparent;
            }}
            QPushButton#runsUploadButton {{
                background: {c["panel3"]};
                border: 1px solid {c["border"]};
                border-radius: 6px; color: {c["muted"]};
                padding: 0; min-width: 24px; max-width: 24px;
                min-height: 24px; max-height: 24px;
            }}
            QPushButton#runsUploadButton:hover {{
                color: {c["text"]}; border-color: {c["accent"]}; background: {c["hover"]};
            }}
            QWidget#workspaceBody {{ background: {c["bg"]}; }}
            QFrame#card {{
                background: {c["panel"]};
                border: 1px solid {c["border"]};
                border-radius: 10px;
            }}
            QLabel#title {{
                font-size: 18px; font-weight: 800; color: {c["text"]};
            }}
            QLabel#sectionLabel {{
                font-size: 10px; font-weight: 800;
                color: {c["muted"]}; letter-spacing: 0.9px; background: transparent;
            }}
            QFrame#inlineStat {{
                background: {c["panel2"]};
                border: 1px solid {c["border2"]};
                border-radius: 7px; min-width: 76px;
            }}
            QLabel#inlineStatLabel {{
                font-size: 9px; font-weight: 800;
                color: {c["muted"]}; letter-spacing: 0.7px; background: transparent;
            }}
            QLabel#inlineStatValue {{
                font-size: 13px; font-weight: 800;
                color: {c["text"]}; background: transparent;
            }}
            QPushButton {{
                background: {c["panel2"]}; color: {c["text"]};
                border: 1px solid {c["border"]};
                border-radius: 8px; padding: 6px 12px; font-weight: 650;
            }}
            QPushButton:hover {{ background: {c["hover"]}; border-color: {c["accent"]}; }}
            QPushButton:pressed {{ background: {c["panel3"]}; padding-top: 7px; padding-bottom: 5px; }}
            QPushButton:disabled {{ color: {c["muted"]}; background: {c["panel2"]}; border-color: {c["border2"]}; }}
            QPushButton#primaryButton {{
                background: {c["accent2"]}; color: {c["atext"]}; border-color: {c["accent2"]};
            }}
            QPushButton#primaryButton:hover {{ background: {c["accent"]}; border-color: {c["accent"]}; }}
            QPushButton#secondaryButton {{ color: {c["muted"]}; }}
            QPushButton#dashRangeBtn {{
                background: transparent; border: 1px solid transparent;
                border-radius: 6px; padding: 4px 10px; font-weight: 700; font-size: 10.5px;
                color: {c["muted"]};
            }}
            QPushButton#dashRangeBtn:checked {{
                background: {c["accent2"]}; color: {c["atext"]}; border-color: {c["accent2"]};
            }}
            QPushButton#dashRangeBtn:hover {{ color: {c["text"]}; }}
            QLineEdit, QComboBox {{
                background: {c["panel2"]}; color: {c["text"]};
                border: 1px solid {c["border"]}; border-radius: 8px;
                padding: 6px 9px;
                selection-background-color: {c["accent2"]};
            }}
            QLineEdit:focus, QComboBox:hover, QComboBox:focus {{
                border-color: {c["accent"]}; background: {c["panel"]};
            }}
            QLineEdit#searchBox {{ padding: 8px 10px; }}
            QComboBox::drop-down {{ width: 22px; border: 0; }}
            QComboBox QAbstractItemView {{
                background: {c["panel"]}; color: {c["text"]};
                border: 1px solid {c["border"]}; border-radius: 8px;
                selection-background-color: {c["sel"]}; padding: 4px; outline: 0;
            }}
            QMenu {{
                background: {c["panel"]}; color: {c["text"]};
                border: 1px solid {c["border"]}; border-radius: 8px; padding: 4px;
            }}
            QMenu::item {{ padding: 7px 22px 7px 10px; border-radius: 6px; }}
            QMenu::item:selected {{ background: {c["sel"]}; color: {c["atext"]}; }}
            QCheckBox {{ spacing: 7px; color: {c["muted"]}; background: transparent; font-weight: 650; }}
            QCheckBox::indicator {{
                width: 15px; height: 15px; border-radius: 4px;
                border: 1px solid {c["border"]}; background: {c["panel2"]};
            }}
            QCheckBox::indicator:hover {{ border-color: {c["accent"]}; }}
            QCheckBox::indicator:checked {{ background: {c["accent2"]}; border-color: {c["accent2"]}; }}
            QListWidget, QTableWidget {{
                background: {c["panel2"]}; color: {c["text"]};
                border: 1px solid {c["border2"]}; border-radius: 8px;
                padding: 4px; outline: 0; gridline-color: transparent;
                alternate-background-color: {c["talt"]};
                selection-background-color: {c["sel"]};
                selection-color: {c["atext"]};
            }}
            QListWidget::item {{
                min-height: 28px; padding: 6px 8px; border-radius: 6px; margin: 1px;
            }}
            QListWidget::item:hover {{ background: {c["hover"]}; }}
            QListWidget::item:selected {{ background: {c["sel"]}; color: {c["atext"]}; }}
            QTableWidget::item {{ padding: 5px; border: 0; }}
            QHeaderView::section {{
                background: {c["panel"]}; color: {c["muted"]}; padding: 8px 6px;
                border: 0; border-bottom: 1px solid {c["border"]}; font-weight: 800;
            }}
            QTableCornerButton::section {{
                background: {c["panel"]}; border: 0; border-bottom: 1px solid {c["border"]};
            }}
            QScrollArea, QScrollArea > QWidget > QWidget {{ background: {c["bg"]}; border: 0; }}
            QSplitter::handle {{ background: {c["border"]}; }}
            QScrollBar:vertical {{
                background: {c["sbg"]}; width: 10px; margin: 2px; border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: {c["sh"]}; min-height: 28px; border-radius: 5px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {c["accent"]}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical,  QScrollBar::sub-page:vertical {{
                height: 0; background: transparent; border: 0;
            }}
            QScrollBar:horizontal {{
                background: {c["sbg"]}; height: 10px; margin: 2px; border-radius: 5px;
            }}
            QScrollBar::handle:horizontal {{
                background: {c["sh"]}; min-width: 28px; border-radius: 5px;
            }}
            QScrollBar::handle:horizontal:hover {{ background: {c["accent"]}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                width: 0; background: transparent; border: 0;
            }}
            QWidget#tabBar     {{ background: {c["tab_bg"]}; border-bottom: 1px solid {c["border"]}; }}
            QWidget#tabContainer {{ background: {c["tab_bg"]}; }}
            QFrame#tabItem {{
                background: {c["tab_bg"]};
                border-right: 1px solid {c["border"]};
                border-bottom: 2px solid transparent;
                min-height: 35px; max-height: 35px;
            }}
            QFrame#tabItem:hover {{ background: {c["hover"]}; }}
            QFrame#tabItem[tabActive="true"] {{
                background: {c["tab_active"]};
                border-bottom: 2px solid {c["tab_border"]};
            }}
            QLabel#tabIcon {{
                font-size: 9px; color: {c["accent"]}; background: transparent;
            }}
            QLabel#tabLabel {{
                font-size: 10pt; color: {c["tab_text"]}; background: transparent;
            }}
            QFrame#tabItem[tabActive="true"] QLabel#tabLabel {{
                color: {c["tab_active_text"]};
            }}
            QPushButton#tabClose {{
                background: transparent; border: none;
                color: {c["muted"]}; font-size: 13px; font-weight: 900;
                padding: 0; border-radius: 3px;
                min-width: 16px; max-width: 16px;
                min-height: 16px; max-height: 16px;
            }}
            QPushButton#tabClose:hover {{ background: #e74c3c; color: #ffffff; }}
            QPushButton#editorSyncButton {{
                background: {c["panel3"]};
                border: 1px solid {c["border"]};
                border-radius: 6px; color: {c["muted"]}; font-size: 13px;
                padding: 0; min-width: 28px; max-width: 28px;
                min-height: 28px; max-height: 28px;
            }}
            QPushButton#editorSyncButton:hover {{
                color: {c["text"]}; border-color: {c["accent"]}; background: {c["hover"]};
            }}
            QPushButton#editorSyncButton:checked {{
                color: {c["accent"]}; border-color: {c["accent"]}; background: {c["panel3"]};
            }}
            QFrame#dropIndicator {{ background: {c["accent"]}; }}
            QWidget#editorArea  {{ background: {c["bg"]}; }}
            QSplitter#editorSplitter::handle  {{ background: {c["border"]}; width: 4px; }}
            QSplitter#chartSplitter::handle   {{ background: {c["border"]}; width: 4px; }}
            QSplitter#contentSplitter::handle {{ background: {c["border"]}; width: 4px; }}
            QFrame#ruleRow {{
                background: {c["panel2"]};
                border: 1px solid {c["border2"]};
                border-radius: 7px;
            }}
            QLabel#monitorPlaceholder {{
                color: {c["muted"]}; font-size: 13pt; background: transparent;
            }}
        """
        self.setStyleSheet(css)
        self.sim_chart.set_dark(self.dark)
        self.editor_area.update_theme(self.dark)
        self.monitor_editor_area.update_theme(self.dark)
        dash_bg = "#111827" if self.dark else "#f8fafc"
        for p in [self._dash_mae_plot, self._dash_ovr_plot]:
            p.setBackground(dash_bg)
        self._rebuild_nav_icons(c)
        # _rebuild_nav_icons resets the bell to its neutral color; re-apply the
        # live chamber-connection indicator so a theme toggle doesn't lose it.
        self.title_bar.set_bell_active(self._any_chamber_connected())
