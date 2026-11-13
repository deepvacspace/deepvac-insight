"""The main application window."""

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal
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

from app.common import (
    ICON_PATH,
    PAGE_AI_AGENTS,
    PAGE_ANALYSIS,
    PAGE_CAMPAIGNS,
    PAGE_CONTROLLER,
    PAGE_OPC,
    PAGE_REPORTS,
    _nav_icon,
    _svg_icon,
)
from app.services import ai_mode, collab_client, i18n_service, local_llm, settings_service
from app.services.chamber_session import MAX_CONNECTED_CHAMBERS, ChamberSession
from app.services.collab_server import CollabServer
from app.services.opc_broadcast_server import OpcBroadcastServer
from app.tab_system import EditorArea
from app.title_bar import TitleBar
from app.views.ai_agents import AiAgentsPage
from app.views.campaigns import CampaignsPage
from app.views.controller import ControllerPage
from app.views.opc import OpcMixin
from app.views.reports import ReportsMixin
from app.views.runs import RunsMixin

NAV_WIDTH = 96


class _GuiCall(QObject):
    """Runs a callable on the GUI thread."""

    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


class _RecorderEvents(QObject):
    """Signals of the experiment recorder for the GUI thread."""

    saved = Signal(str, str)
    failed = Signal(str)


class DeepVacDesktop(
    RunsMixin,
    ReportsMixin,
    OpcMixin,
    QMainWindow,
):
    def __init__(self, splash=None, current_user=None):
        super().__init__()
        self.splash = splash
        self.current_user = current_user or {"id": None, "name": "User", "email": ""}
        self.logout_requested = False
        self.setWindowTitle(self.tr("Deepvac Dashboard"))
        self.setWindowIcon(QIcon(ICON_PATH))
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(1500, 920)
        self.setMinimumSize(1100, 700)

        self.runs = []
        self.dark = settings_service.load_theme()

        self._chamber_sessions = {}
        self.opc_server = OpcBroadcastServer(self)
        self.collab_server = CollabServer()
        from app.api.chambers import ChamberLinks

        self.plc_links = ChamberLinks()
        from app.services.experiment_recorder import ExperimentRecorder

        self._recorder_events = _RecorderEvents(self)
        self._recorder_events.saved.connect(self._on_experiment_saved)
        self._recorder_events.failed.connect(self._on_experiment_save_failed)
        self.experiment_recorder = ExperimentRecorder(
            configuration=self._job_configuration,
            channels=self._recording_channels,
            on_saved=self._recorder_events.saved.emit,
            on_failed=self._recorder_events.failed.emit,
        )
        self.day_recorder = None
        self._recording_feed = None
        self._start_day_recording()

        self.local_model = local_llm.LocalModelServer()
        from app.services.demo_launcher import DemoLauncher

        self._gui_call = _GuiCall()
        self.demo = DemoLauncher(self)
        from app.services.demo_autoplay import AutoPlay

        self.autoplay = AutoPlay(self, parent=self)
        self._build_ui()
        from app.agent_chat import AgentChat

        self.agent_chat = AgentChat(
            self.centralWidget(),
            api_source=self._agent_api_source,
            context_source=self._ai_context,
        )
        if ai_mode.is_offline():
            self.local_model.ensure()

        self.apply_theme()
        self._open_live_tab()
        self.load_runs()
        self._open_live_tab()
        self.experiment_recorder.recover_interrupted()
        self._start_collab_host_if_enabled()

        self._backup_timer = QTimer(self)
        self._backup_timer.timeout.connect(self._run_background_backup)
        self._backup_timer.start(6 * 60 * 60 * 1000)

        self.agent_api = None
        self._agent_requests_dialog = None
        self._known_pending_requests = 0
        self._known_open_holds = 0
        if settings_service.load_agent_api_enabled():
            self._start_agent_api(show_errors=False)
        self._agent_requests_timer = QTimer(self)
        self._agent_requests_timer.timeout.connect(self._check_agent_requests)
        self._agent_requests_timer.start(5000)

    def _run_background_backup(self):
        from app.services import backup_service

        try:
            backup_service.backup_all()
        except Exception as exc:
            print(f"[backup] periodic backup skipped: {exc}")

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
        session.sample_received.connect(
            lambda sample, s=session: self._record_session_sample(s, sample)
        )
        for signal in (
            session.manual_state_changed,
            session.test_state_changed,
            session.disconnected,
        ):
            signal.connect(lambda *_args, s=session: self._session_job_changed(s))
        for signal in (
            session.connected,
            session.disconnected,
            session.connection_error,
            session.reconnecting,
            session.reconnect_failed,
        ):
            signal.connect(lambda *_args: self._refresh_connections_dialog())

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

    def _refresh_chamber_ui(self):
        """Syncs the chamber-dependent widgets."""
        self._refresh_connections_dialog()
        if hasattr(self, "controller_widget"):
            self.controller_widget.reload_chambers()
        live = self.editor_area.page_for("live") if hasattr(self, "editor_area") else None
        if live is not None:
            live.reload_chambers()
        self._recompute_chamber_aggregate_status()

    def _refresh_connections_dialog(self):
        dialog = getattr(self, "_connections_dialog", None)
        if dialog is not None:
            dialog.refresh()

    def _show_chamber_connections(self):
        from app.chamber_connections_dialog import ChamberConnectionsDialog

        self._connections_dialog = ChamberConnectionsDialog(self)
        try:
            self._connections_dialog.exec()
        finally:
            self._connections_dialog = None

    def _recompute_chamber_aggregate_status(self):
        count = self._connected_chamber_count()
        self.title_bar.set_chamber_status(count, MAX_CONNECTED_CHAMBERS)
        self._opc_set_chamber_connected(count > 0)

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
        self.collab_server.stop()
        self._stop_agent_api()
        self._stop_day_recording()
        self.experiment_recorder.close()
        self.plc_links.close()
        for session in list(self._chamber_sessions.values()):
            session.teardown()
        self.ai_agents_widget.shutdown()
        self.local_model.stop()
        self.autoplay.set_on(False)
        self.demo.stop()
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

    def _build_ui(self):
        central = QWidget()
        window_lay = QHBoxLayout(central)
        window_lay.setContentsMargins(0, 0, 0, 0)
        window_lay.setSpacing(0)
        window_lay.addWidget(self._sidebar())
        right = QWidget()
        outer = QVBoxLayout(right)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        window_lay.addWidget(right, 1)
        self.title_bar = TitleBar(self, show_logo=False)
        outer.addWidget(self.title_bar)

        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        self.content_stack = QStackedWidget()
        self.editor_area = EditorArea()
        self.editor_area.active_page_changed.connect(self._on_active_page_changed)
        self._build_runs_drawer()
        self.reports_widget = self._reports_view()
        self.controller_widget = ControllerPage(
            self.plc_links, self.current_user, self.experiment_recorder
        )
        self.opc_widget = self._opc_view()
        self.campaigns_widget = CampaignsPage(
            self.current_user, lambda: self.agent_api is not None and self.agent_api.supervisor
        )
        self.ai_agents_widget = AiAgentsPage(host=self)
        for w in [
            self.editor_area,
            self.reports_widget,
            self.controller_widget,
            self.opc_widget,
            self.campaigns_widget,
            self.ai_agents_widget,
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
        self._install_fullscreen_keys()
        self._nav_to(PAGE_ANALYSIS)

    def _sidebar(self):
        """Builds the navigation bar."""
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QFrame, QLabel, QToolButton, QVBoxLayout

        from app.common import LOGO_WHITE_PATH
        from app.nav_status import NavStatus

        bar = QFrame()
        bar.setObjectName("activityBar")
        bar.setFixedWidth(NAV_WIDTH)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(6, 0, 6, 8)
        lay.setSpacing(4)
        logo = QLabel()
        logo.setObjectName("navLogo")
        logo.setFixedHeight(40)
        logo.setAlignment(Qt.AlignCenter)
        pix = QPixmap(LOGO_WHITE_PATH)
        if not pix.isNull():
            logo.setPixmap(pix.scaledToWidth(NAV_WIDTH - 20, Qt.SmoothTransformation))
        lay.addWidget(logo)
        lay.addSpacing(4)

        def nav_button(label, icon_name, checkable=True):
            btn = QToolButton()
            btn.setObjectName("navButton")
            btn.setCheckable(checkable)
            btn.setText(label)
            btn.setToolTip(label)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setFixedSize(NAV_WIDTH - 12, 60)
            btn.setIconSize(QSize(26, 26))
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setProperty("nav_icon", icon_name)
            return btn

        nav_defs = [
            ("act_analysis_btn", "nav-analysis", self.tr("Analysis"), PAGE_ANALYSIS),
            ("act_reports_btn", "file-earmark", self.tr("Reports"), PAGE_REPORTS),
            ("act_controller_btn", "nav-control", self.tr("Control"), PAGE_CONTROLLER),
            ("act_opc_btn", "broadcast", self.tr("OPC Server"), PAGE_OPC),
            ("act_campaigns_btn", "clipboard-check", self.tr("Campaigns"), PAGE_CAMPAIGNS),
            ("act_ai_agents_btn", "nav-agents", self.tr("AI Agents"), PAGE_AI_AGENTS),
        ]
        hidden_pages = {PAGE_REPORTS, PAGE_OPC, PAGE_CAMPAIGNS}
        self._nav_buttons = []
        for attr, icon_name, label, idx in nav_defs:
            btn = nav_button(label, icon_name)
            btn.clicked.connect(lambda _checked=False, i=idx: self._nav_to(i))
            setattr(self, attr, btn)
            self._nav_buttons.append(btn)
            lay.addWidget(btn, 0, Qt.AlignHCenter)
            btn.setVisible(idx not in hidden_pages)

        lay.addStretch(1)

        self.nav_status = NavStatus()
        lay.addWidget(self.nav_status)
        self.nav_clock = QLabel()
        self.nav_clock.setObjectName("navClock")
        self.nav_clock.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.nav_clock)
        lay.addSpacing(4)
        self._nav_timer = QTimer(self)
        self._nav_timer.timeout.connect(self._update_nav_status)
        self._nav_timer.start(1000)

        self.act_account_btn = nav_button(self.tr("Account"), "nav-account", checkable=False)
        self.act_account_btn.setToolTip(self.current_user.get("name") or self.tr("Account"))
        self.act_account_btn.clicked.connect(self._show_account_menu)
        lay.addWidget(self.act_account_btn, 0, Qt.AlignHCenter)

        self.act_settings_btn = nav_button(self.tr("Settings"), "nav-settings", checkable=False)
        self.act_settings_btn.clicked.connect(self._show_settings)
        lay.addWidget(self.act_settings_btn, 0, Qt.AlignHCenter)

        return bar

    def toggle_fullscreen(self):
        """Toggles full screen."""
        if self.isFullScreen():
            if getattr(self, "_maximized_before_fullscreen", False):
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self._maximized_before_fullscreen = self.isMaximized()
            self.showFullScreen()
        self._esc_shortcut.setEnabled(self.isFullScreen())

    def _install_fullscreen_keys(self):
        from PySide6.QtGui import QKeySequence, QShortcut

        QShortcut(QKeySequence(Qt.Key_F11), self, activated=self.toggle_fullscreen)
        self._esc_shortcut = QShortcut(
            QKeySequence(Qt.Key_Escape), self, activated=self.toggle_fullscreen
        )
        self._esc_shortcut.setEnabled(False)

    def _update_nav_status(self):
        """Updates the navigation bar's chamber status."""
        from datetime import datetime

        self.nav_clock.setText(datetime.now().strftime("%H:%M:%S"))
        try:
            chambers = [c for c in self.plc_links.chambers() if c.get("protocol") == "plc"]
        except Exception:
            chambers = []
        demo_id = getattr(self.demo, "chamber_id", None)
        chamber = next(
            (c for c in chambers if c["id"] == demo_id), chambers[0] if chambers else None
        )
        if chamber is None:
            self.nav_status.show_chamber(None, None)
            return
        try:
            snapshot = self.plc_links.client(chamber, wait=False).snapshot()
        except Exception:
            snapshot = None
        if getattr(self, "_nav_pres_unit", None) is None:
            self._nav_pres_unit = (self._recording_labels(None).get("pres") or {}).get(
                "unit"
            ) or "Torr"
        self.nav_status.show_chamber(chamber["name"], snapshot, self._nav_pres_unit)

    def _rebuild_nav_icons(self, c: dict):
        for btn in self._nav_buttons:
            name = btn.property("nav_icon")
            if name:
                btn.setIcon(_nav_icon(name, c["nav_icon"], c["nav_sel_icon"], 26))
        self.act_account_btn.setIcon(_svg_icon("nav-account", c["nav_icon"], 26))
        self.act_settings_btn.setIcon(_svg_icon("nav-settings", c["nav_icon"], 26))

    def _nav_to(self, index):
        for i, btn in enumerate(self._nav_buttons):
            btn.setChecked(i == index)
        self.content_stack.setCurrentIndex(index)
        if hasattr(self, "agent_chat"):
            self.agent_chat.set_available(index != PAGE_AI_AGENTS)
        if index == PAGE_AI_AGENTS and hasattr(self, "demo"):
            self._ensure_demo_started()
        if index != PAGE_ANALYSIS and hasattr(self, "runs_drawer"):
            self.runs_drawer.close()
        if index == PAGE_REPORTS:
            self._refresh_reports()

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
        act_fullscreen = menu.addAction(self.tr("Full Screen\tF11"))
        act_fullscreen.setCheckable(True)
        act_fullscreen.setChecked(self.isFullScreen())

        menu.addSeparator()
        act_connections = menu.addAction(self.tr("Chamber Connections…"))

        menu.addSeparator()
        act_backup_now = menu.addAction(self.tr("Back Up Now"))
        act_open_backups = menu.addAction(self.tr("Open Backups Folder"))

        menu.addSeparator()
        pending = self._pending_agent_requests()
        act_agent_requests = menu.addAction(
            self.tr("Agent Requests ({0} pending)…").format(pending)
            if pending
            else self.tr("Agent Requests…")
        )
        wiki_pending = self._pending_wiki_proposals()
        act_wiki_proposals = menu.addAction(
            self.tr("Wiki Proposals ({0} pending)…").format(wiki_pending)
            if wiki_pending
            else self.tr("Wiki Proposals…")
        )
        act_wiki_folder = menu.addAction(self.tr("Open Wiki Folder"))
        new_events = self._new_observer_events()
        act_observer = menu.addAction(
            self.tr("Observer Events ({0} new)…").format(new_events)
            if new_events
            else self.tr("Observer Events…")
        )
        act_agent_api = menu.addAction(self.tr("Agent API"))
        act_agent_api.setCheckable(True)
        act_agent_api.setChecked(self.agent_api is not None)
        act_agent_token = menu.addAction(self.tr("Copy Agent API Token"))
        act_agent_token.setEnabled(self.agent_api is not None)
        act_agent_chat_url = menu.addAction(self.tr("Agent Chat URL…"))
        mode_menu = menu.addMenu(self.tr("AI Mode"))
        current_mode = ai_mode.mode()
        act_mode_online = mode_menu.addAction(self.tr("Online: Claude through the Deepvac Agents"))
        act_mode_offline = mode_menu.addAction(self.tr("Offline: local model on this PC"))
        for act, value in ((act_mode_online, ai_mode.ONLINE), (act_mode_offline, ai_mode.OFFLINE)):
            act.setCheckable(True)
            act.setChecked(current_mode == value)
        act_live_tab = menu.addAction(self.tr("Open Live Tab"))

        menu.addSeparator()
        act_recordings_folder = menu.addAction(self.tr("Open Recordings Folder"))

        btn = self.act_settings_btn
        chosen = menu.exec(btn.mapToGlobal(QPoint(btn.width() + 4, 0)))
        if chosen in (act_dark, act_light) and (chosen == act_dark) != self.dark:
            self.dark = chosen == act_dark
            settings_service.save_theme(self.dark)
            self.apply_theme()
        elif chosen == act_fullscreen:
            self.toggle_fullscreen()
        elif chosen == act_connections:
            self._show_chamber_connections()
        elif chosen == act_backup_now:
            self._backup_now()
        elif chosen == act_open_backups:
            self._open_backups_folder()
        elif chosen == act_agent_requests:
            self._show_agent_requests()
        elif chosen == act_wiki_proposals:
            self._show_wiki_proposals()
        elif chosen == act_wiki_folder:
            self._open_wiki_folder()
        elif chosen == act_observer:
            self._show_observer_events()
        elif chosen == act_agent_api:
            self._toggle_agent_api(act_agent_api.isChecked())
        elif chosen == act_agent_token:
            self._copy_agent_api_token()
        elif chosen == act_agent_chat_url:
            self.agent_chat.configure_url(self)
        elif chosen in (act_mode_online, act_mode_offline):
            self.set_ai_mode(ai_mode.OFFLINE if chosen == act_mode_offline else ai_mode.ONLINE)
        elif chosen == act_live_tab:
            self._open_live_tab()
            self._nav_to(PAGE_ANALYSIS)
        elif chosen == act_recordings_folder:
            self._open_recordings_folder()
        elif chosen in lang_actions:
            self._change_language(lang_actions[chosen])

    def _start_day_recording(self):
        from app.services.day_recorder import DayRecorder, RecordingFeed

        self.day_recorder = DayRecorder(labels=self._recording_labels)
        self._recording_feed = RecordingFeed(
            self.plc_links, self.day_recorder, self.experiment_recorder
        )
        self._recording_feed.start()

    def _stop_day_recording(self):
        if self._recording_feed is not None:
            self._recording_feed.stop()
        self.day_recorder = None
        self._recording_feed = None

    @staticmethod
    def _session_working(session):
        return session.manual_running or session.test_running_profile is not None

    def _record_session_sample(self, session, sample):
        """Records a sample-feed chamber's sample."""
        name = session.chamber["name"]
        if self.day_recorder is not None:
            self.day_recorder.add(name, sample)
        self.experiment_recorder.add(name, sample, self._session_working(session))

    def _session_job_changed(self, session):
        """A sample-feed chamber started or stopped a manual setpoint / test profile."""
        name = session.chamber["name"]
        if not self._session_working(session):
            self.experiment_recorder.set_working(name, False)
            return
        if self.experiment_recorder.running(name) is None:
            profile = session.test_running_profile
            if profile is not None:
                self.experiment_recorder.set_next_job(
                    name, f"Test profile {profile['name']}", "test_profile", profile["name"]
                )
            else:
                self.experiment_recorder.set_next_job(name, "Manual setpoint", "manual_setpoint")

    def _job_configuration(self, chamber_name):
        """Returns the configuration snapshot of a job."""
        from app.api import control_settings

        settings = control_settings.load()
        return {
            "plc_settings": settings.get("plc_settings") or {},
            "control_params": control_settings.control_params(settings),
            "alarms": settings.get("alarms") or [],
            "app": {"name": "Deepvac Insight", "version": _app_version()},
            "user": {"name": self.current_user.get("name") or ""},
        }

    def _recording_channels(self, chamber_name):
        meta = self._recording_labels(chamber_name)
        return [
            {"name": name, "label": m.get("label", ""), "unit": m.get("unit", "")}
            for name, m in meta.items()
        ]

    def _on_experiment_saved(self, key, name):
        from app.services import data_service

        self.runs = data_service.load_cached_runs()
        self.render_runs()
        live = self.editor_area.page_for("live")
        if live is not None:
            live.show_notice(self.tr('Experiment saved as run "{0}".').format(name))

    def _on_experiment_save_failed(self, message):
        live = self.editor_area.page_for("live")
        if live is not None:
            live.show_notice(
                self.tr(
                    "An experiment could not be saved ({0}); it is retried at the next start."
                ).format(message),
                error=True,
            )

    @staticmethod
    def _recording_labels(chamber_name):
        """Returns the channel descriptions of a day file."""
        from app.api import control_settings

        try:
            return control_settings.state_meta(control_settings.load())
        except Exception:
            return {}

    def _open_recordings_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from app.services import day_recorder

        folder = day_recorder.recordings_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _open_live_tab(self):
        from app.live_tab import LIVE_KEY, LiveTabPage

        page = self.editor_area.page_for(LIVE_KEY)
        if page is None:
            page = LiveTabPage(
                self.plc_links,
                dark=self.dark,
                sessions=lambda: self._chamber_sessions,
                recorder=lambda: self.day_recorder,
            )
            page.run_saved.connect(self._on_live_run_saved)
        self.editor_area.open_run(LIVE_KEY, self.tr("● Live"), page)

    def _on_live_run_saved(self, _key):
        from app.services import data_service

        self.runs = data_service.load_cached_runs()
        self.render_runs()

    def _start_agent_api(self, show_errors=True):
        """Starts the agent API."""
        import logging

        from PySide6.QtWidgets import QMessageBox

        from app.api.server import AgentApi

        try:
            api = AgentApi(links=self.plc_links)
            api.start()
            if not api.wait_started():
                api.stop()
                raise RuntimeError(
                    self.tr("Port {0} may be in use by another program.").format(api.port)
                )
        except Exception as exc:
            self.agent_api = None
            message = self.tr("The Agent API did not start: {0}").format(exc)
            if show_errors:
                QMessageBox.warning(self, self.tr("Agent API"), message)
            else:
                logging.getLogger(__name__).warning(message)
            return False
        self.agent_api = api
        return True

    def _stop_agent_api(self):
        if self.agent_api is not None:
            self.agent_api.stop()
            self.agent_api = None

    def start_demo(self, mode):
        self.demo.start(mode, gui_call=self._gui_call.call.emit)

    def _ensure_demo_started(self):
        """Starts the demo the first time AI Agents opens."""
        if not (self.demo.running or self.demo.starting):
            self.start_demo(ai_mode.mode())

    def set_ai_mode(self, mode):
        """Sets the AI mode, online or offline."""
        ai_mode.set_mode(mode)
        self.agent_chat.panel.refresh_mode()
        if mode == ai_mode.OFFLINE:
            self.local_model.ensure()

    def _ai_context(self):
        """Returns the current state as text for the local model."""
        import time as _time

        from app.services import campaign_service as cs

        lines = [f"Now: {_time.strftime('%Y-%m-%d %H:%M')}."]
        for chamber in self.plc_links.chambers():
            try:
                snap = self.plc_links.client(chamber, wait=False).snapshot()
            except Exception:
                continue
            v = snap.values or {}
            fresh = snap.connected and snap.values_at and _time.time() - snap.values_at < 15
            if not fresh:
                lines.append(f"Chamber {chamber['name']}: not connected.")
                continue
            duts = ", ".join(
                f"temp{i} {v[f'temp{i}']:.1f}"
                for i in range(1, 5)
                if isinstance(v.get(f"temp{i}"), float) and v[f"temp{i}"] == v[f"temp{i}"]
            )
            pres = v.get("pres")
            lines.append(
                f"Chamber {chamber['name']}: {snap.status}, {v.get('temp', float('nan')):.2f} °C, "
                f"setpoint {v.get('temp_ref', float('nan')):.1f} °C"
                + (f", pressure {pres:.2e} Torr" if isinstance(pres, float) else "")
                + (f", test-article thermocouples {duts} °C" if duts else "")
                + "."
            )
        campaign = cs.latest_campaign()
        if campaign is not None:
            public = cs.public(campaign)
            lines.append(
                f"Campaign {public['id']} ({public['title']}): {public['state']}, {public['specification']}."
            )
            if public.get("step"):
                st = public["step"]
                lines.append(
                    f"Step {st['index']}/{st['of']}: {st['label']} ({st.get('status', '')})."
                )
            hold = public.get("hold_point")
            if hold:
                options = ", ".join(o["label"] for o in hold["options"])
                rec = hold.get("recommendation")
                lines.append(
                    f"Decision point waiting: {hold['title']} {hold['detail']} Options: {options}."
                )
                if rec:
                    lines.append(f"{rec['by']} recommends {rec['option']}: {rec['rationale']}")
            for r in (campaign["results"] or [])[-8:]:
                lines.append(
                    f"Plateau cycle {r['cycle']} {r['plateau']}: {r['min_c']:.2f} to {r['max_c']:.2f} °C, "
                    f"{r['dwell_min']} min."
                )
            for d in (campaign["deviations"] or [])[-4:]:
                lines.append(f"Deviation ({d['where']}): {d['text']} {d.get('decision', '')}")
            lines.append("Recent signed log:")
            for e in cs.log_entries(campaign["id"])[-10:]:
                lines.append(f"- {e['actor']}: {e['action']}: {e['detail'][:200]}")
        runs = (self.runs or [])[:5]
        if runs:
            lines.append(
                "Latest runs: " + "; ".join(str(r.get("id") or r.get("name")) for r in runs)
            )
        return "\n".join(lines)

    def _toggle_agent_api(self, enabled, quiet=False):
        """Turns the agent API on or off."""
        from PySide6.QtWidgets import QMessageBox

        if enabled:
            if self._start_agent_api():
                settings_service.save_agent_api_enabled(True)
                if quiet:
                    return
                QMessageBox.information(
                    self,
                    self.tr("Agent API"),
                    self.tr(
                        "The Agent API is running on {0}.\n\nAnalyst: {0}/analyst/mcp\nWorker: {0}/worker/mcp\n\n"
                        "Use Copy Agent API Token to give n8n its token."
                    ).format(self.agent_api.url),
                )
        else:
            self._stop_agent_api()
            settings_service.save_agent_api_enabled(False)

    def _agent_api_source(self):
        """Returns the agent API address and token for the chat."""
        if self.agent_api is not None:
            return self.agent_api.url, self.agent_api.token
        from app.api.auth import DEFAULT_URL, api_token

        return DEFAULT_URL, api_token()

    def _copy_agent_api_token(self):
        from PySide6.QtWidgets import QApplication

        if self.agent_api is not None:
            QApplication.clipboard().setText(self.agent_api.token)

    def _pending_agent_requests(self):
        from app.services import agent_requests_service

        try:
            return agent_requests_service.pending_count()
        except Exception:
            return 0

    def _check_agent_requests(self):
        """Opens Agent Requests when a new request waits."""
        pending = self._pending_agent_requests()
        if pending > self._known_pending_requests:
            self._show_agent_requests()
        self._known_pending_requests = pending
        holds = self._open_campaign_holds()
        if holds > self._known_open_holds:
            self._nav_to(PAGE_AI_AGENTS)
            self.raise_()
            self.activateWindow()
        self._known_open_holds = holds

    def _open_campaign_holds(self):
        from app.services import campaign_service

        try:
            return campaign_service.open_holds_count()
        except Exception:
            return 0

    def _pending_wiki_proposals(self):
        from app.services import wiki_service

        try:
            return len(wiki_service.list_proposals(wiki_service.PENDING))
        except Exception:
            return 0

    def _show_wiki_proposals(self):
        from app.wiki_proposals_dialog import WikiProposalsDialog

        WikiProposalsDialog(self.current_user, self).exec()

    def _open_wiki_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from app.services import wiki_service

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(wiki_service.wiki_dir())))

    def _new_observer_events(self):
        from app.services import observer_service

        try:
            return observer_service.unacknowledged_count()
        except Exception:
            return 0

    def _show_observer_events(self):
        from app.observer_dialog import ObserverDialog

        ObserverDialog(self.current_user, self).exec()

    def _show_agent_requests(self):
        from app.agent_requests_dialog import AgentRequestsDialog

        if self._agent_requests_dialog is None:
            self._agent_requests_dialog = AgentRequestsDialog(self.current_user, self)
            self._agent_requests_dialog.finished.connect(
                lambda _result: setattr(self, "_agent_requests_dialog", None)
            )
            self._agent_requests_dialog.show()
        self._agent_requests_dialog.raise_()
        self._agent_requests_dialog.activateWindow()

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
            self.tr("The new language will take effect the next time you start Deepvac."),
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

    def _show_account_menu(self):
        menu = QMenu(self)
        if self.current_user.get("hub_user_id") and self.current_user.get("hub_org_name"):
            org_header = menu.addAction(self.current_user["hub_org_name"])
            org_header.setEnabled(False)
        header = menu.addAction(self.current_user.get("name") or self.tr("Account"))
        header.setEnabled(False)
        menu.addSeparator()
        act_profile = menu.addAction(self.tr("Profile"))
        act_directory = menu.addAction(self.tr("Organization Directory"))
        act_collab = menu.addAction(self.tr("Collaboration"))
        act_logout = menu.addAction(self.tr("Log out"))
        btn = self.act_account_btn
        chosen = menu.exec(btn.mapToGlobal(QPoint(btn.width() + 4, 0)))
        if chosen == act_profile:
            self._show_profile_dialog()
        elif chosen == act_directory:
            self._show_org_directory_dialog()
        elif chosen == act_collab:
            self._show_collab_dialog()
        elif chosen == act_logout:
            self._logout()

    def _show_profile_dialog(self):
        from app.profile_dialog import ProfileDialog

        dlg = ProfileDialog(self.current_user, self)
        dlg.exec()
        self.current_user = dlg.updated_user
        self.act_account_btn.setToolTip(self.current_user.get("name") or self.tr("Account"))

    def _show_org_directory_dialog(self):
        from app.org_directory_dialog import OrgDirectoryDialog

        dlg = OrgDirectoryDialog(self)
        dlg.exec()

    def _show_collab_dialog(self):
        from app.collab_dialog import CollabDialog

        dlg = CollabDialog(self.collab_server, self.current_user, self)
        dlg.exec()

    def _start_collab_host_if_enabled(self):
        if not settings_service.load_collab_host_enabled():
            return
        try:
            self.collab_server.start(settings_service.load_collab_port())
        except OSError:
            return
        collab_client.connect_local(
            self.collab_server.port,
            self.collab_server.host_name,
            self.current_user.get("name") or "Unknown",
            self.collab_server.cert_pem,
        )

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

    def splash_msg(self, msg):
        from PySide6.QtWidgets import QApplication

        if self.splash:
            self.splash.showMessage(msg, Qt.AlignCenter | Qt.AlignBottom, Qt.white)
            QApplication.processEvents()

    def apply_theme(self):
        """Applies the light or dark theme."""
        from app.theme_colors import HMI

        h = HMI
        nav = {
            "nav_bg": h["slate"],
            "nav_icon": h["sky"],
            "nav_hover": h["slate_raised"],
            "nav_sel": h["fog"],
            "nav_sel_icon": h["cobalt"],
            "nav_text": h["pearl"],
            "nav_text2": h["haze"],
            "nav_box": h["slate_raised"],
            "nav_line": h["slate_line"],
            "ok": h["live"],
            "warn": h["connecting"],
            "danger": h["alarm"],
            "danger_hover": h["alarm_pressed"],
            "accent_pressed": h["cobalt_pressed"],
            "accent_hover": h["cobalt_pressed"],
            "atext": "#ffffff",
        }
        nav.update(
            {
                "warn_tint": "#4A3A15",
                "warn_text": h["connecting"],
                "ok_tint": "#173D2E",
                "ok_text": h["live"],
                "crit_tint": "#4A1D25",
            }  # fmt: skip
            if self.dark
            else {
                "warn_tint": "#FDF0D6",
                "warn_text": "#9A6A06",
                "ok_tint": "#DDF4E9",
                "ok_text": "#12814E",
                "crit_tint": "#FADBDF",
            }  # fmt: skip
        )
        if self.dark:
            c = {
                "bg": h["slate"],
                "panel": h["slate_raised"],
                "panel2": h["slate_raised"],
                "panel3": h["slate_line"],
                "text": h["pearl"],
                "muted": h["haze"],
                "border": h["slate_line"],
                "border2": h["slate_line"],
                "accent": h["sky"],
                "accent2": h["cobalt"],
                "hover": h["slate_line"],
                "sel": h["slate_line"],
                "sel_text": h["pearl"],
                "talt": h["slate"],
                "sbg": h["slate"],
                "sh": h["slate_line"],
                "tab_bg": h["slate"],
                "tab_active": h["slate_raised"],
                "tab_border": h["sky"],
                "tab_text": h["haze"],
                "tab_active_text": h["pearl"],
                **nav,
            }
        else:
            c = {
                "bg": h["plot"],
                "panel": h["white"],
                "panel2": h["plot"],
                "panel3": h["fog"],
                "text": h["slate"],
                "muted": h["steel"],
                "border": h["border"],
                "border2": h["grid"],
                "accent": h["cobalt"],
                "accent2": h["cobalt"],
                "hover": h["plot"],
                "sel": h["cobalt_tint"],
                "sel_text": h["slate"],
                "talt": h["plot"],
                "sbg": h["plot"],
                "sh": h["haze"],
                "tab_bg": h["fog"],
                "tab_active": h["white"],
                "tab_border": h["cobalt"],
                "tab_text": h["steel"],
                "tab_active_text": h["slate"],
                **nav,
            }

        css = f"""
            QWidget {{
                background: {c["bg"]};
                color: {c["text"]};
                font-family: "Segoe UI", "Inter", "Arial";
                font-size: 10.5pt;
            }}
            QWidget#titleBar {{
                background: {c["nav_bg"]};
                border: none;
                min-height: 40px; max-height: 40px;
            }}
            QWidget#titleBar QLabel {{ background: transparent; }}
            QWidget#titleBar QLabel#statusText {{ color: {c["nav_text2"]}; }}
            QWidget#titleBar QPushButton#winBtn:hover {{ background: {c["nav_hover"]}; }}
            QWidget#titleBarLogoArea {{
                background: transparent;
            }}
            QLabel#titleBarBrand {{
                font-size: 11px; font-weight: 900;
                color: {c["accent"]};
                background: transparent;
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
                background: {c["danger"]};
                color: #ffffff;
            }}
            QFrame#titleStatusPill {{
                background: transparent;
                border: none;
                min-height: 28px; max-height: 28px;
            }}
            QLabel#sysDotOk {{
                color: {c["ok"]}; font-size: 9px; background: transparent;
            }}
            QLabel#sysDotErr {{
                color: {c["danger"]}; font-size: 9px; background: transparent;
            }}
            QLabel#chamberIconOn {{
                color: {c["ok"]}; font-size: 11px; background: transparent;
            }}
            QLabel#chamberIconOff {{
                color: {c["danger"]}; font-size: 11px; background: transparent;
            }}
            QLabel#statusText {{
                font-size: 9.5pt; color: {c["muted"]}; background: transparent;
            }}
            QPushButton#titleIconBtn {{
                background: transparent; border: none;
                border-radius: 4px; font-size: 14px; padding: 0;
            }}
            QPushButton#titleIconBtn:hover {{ background: {c["hover"]}; }}
            QFrame#activityBar {{ background: {c["nav_bg"]}; border: none; }}
            QToolButton#navButton {{
                background: transparent; border: none; border-radius: 10px;
                color: {c["nav_text"]}; font-size: 8.5pt; padding: 6px 0 4px 0;
            }}
            QToolButton#navButton:hover {{ background: {c["nav_hover"]}; }}
            QToolButton#navButton:checked {{
                background: {c["nav_sel"]}; color: {c["nav_bg"]}; font-weight: bold;
            }}
            QFrame#navStatus {{
                background: {c["nav_box"]}; border: 1px solid {c["nav_icon"]}; border-radius: 10px;
            }}
            QFrame#navStatus QLabel {{ background: transparent; }}
            QLabel#navStatusState {{ color: {c["nav_text"]}; font-size: 9pt; }}
            QLabel#navStatusJob {{ color: {c["nav_text2"]}; font-size: 8pt; }}
            QLabel#navStatusUnit {{ color: {c["nav_icon"]}; font-size: 8pt; }}
            QLabel#navStatusValue {{ color: {c["nav_text"]}; font-size: 10.5pt; font-weight: bold; }}
            QFrame#navStatusLine {{ background: {c["nav_line"]}; border: none; }}
            QLabel#navLogo {{ background: transparent; }}
            QLabel#navClock {{ color: {c["nav_text"]}; font-size: 10.5pt; background: transparent; }}
            QLabel#pageTitle {{
                font-size: 22px; font-weight: 800;
                color: {c["text"]}; background: transparent;
            }}
            QWidget#runsPanel, QFrame#drawerPanel, QWidget#drawerBody {{
                background: {c["panel"]};
            }}
            QFrame#drawerPanel {{ border: 1px solid {c["border"]}; }}
            QFrame#drawerPanel QScrollArea, QFrame#drawerPanel QScrollArea > QWidget > QWidget {{
                background: {c["panel"]};
            }}
            QWidget#aiAgentsBar {{
                background: {c["panel"]}; border-bottom: 1px solid {c["border2"]};
            }}
            QWidget#aiAgentsBar QLabel {{ background: transparent; }}
            QWidget#agentsPlain, QScrollArea#agentsScroll, QScrollArea#stepList {{
                background: transparent; border: 0;
            }}
            QScrollArea#agentsScroll > QWidget > QWidget, QScrollArea#stepList > QWidget > QWidget {{
                background: transparent;
            }}
            QWidget#aiAgentsPage QLabel {{ background: transparent; }}
            QFrame#stageRail {{
                background: {c["panel2"]}; border: 1px solid {c["border"]}; border-radius: 10px;
            }}
            QLabel#stageSeg {{ color: {c["muted"]}; padding: 7px 6px; border-radius: 7px; font-size: 10.5pt; }}
            QLabel#stageSeg[state="done"] {{ color: {c["nav_text2"]}; }}
            QLabel#stageSeg[state="now"] {{
                background: {c["panel"]}; color: {c["text"]}; font-weight: bold; border: 1px solid {c["border"]};
            }}
            QFrame#aiBanner {{ background: {c["warn_tint"]}; border-radius: 8px; }}
            QLabel#bannerText {{ color: {c["warn_text"]}; }}
            QLabel#planTitle {{ font-size: 13.5pt; font-weight: 800; color: {c["text"]}; }}
            QLabel#chartTitle, QLabel#agentsHeading {{ font-size: 11pt; font-weight: 800; color: {c["text"]}; }}
            QLabel#chamberHeadText {{ font-size: 11pt; font-weight: 700; color: {c["text"]}; }}
            QLabel#runClock {{ font-size: 14pt; font-weight: 700; color: {c["text"]}; }}
            QLabel#fieldCaption, QLabel#tileHead {{ color: {c["muted"]}; font-size: 9pt; }}
            QLabel#formError {{ color: {c["danger"]}; }}
            QLabel#chartLegend {{ font-size: 9pt; }}
            QLabel#statusChip {{
                background: {c["warn_tint"]}; color: {c["warn_text"]}; border-radius: 11px;
                padding: 3px 10px; font-weight: 700;
            }}
            QLabel#statusChip[ok="true"] {{ background: {c["ok_tint"]}; color: {c["ok_text"]}; }}
            QFrame#specGrid {{ background: {c["border"]}; border-radius: 8px; }}
            QFrame#specCell {{ background: {c["panel"]}; }}
            QLabel#cellName {{ color: {c["muted"]}; font-size: 8.5pt; }}
            QLabel#cellValue {{ color: {c["text"]}; font-size: 10.5pt; font-weight: 700; }}
            QLabel#cellSource {{ color: {c["muted"]}; font-size: 8.5pt; }}
            QScrollArea#stepList {{ border: 1px solid {c["border"]}; border-radius: 10px; }}
            QFrame#stepRow {{ background: transparent; border: 0; border-top: 1px solid {c["border2"]}; }}
            QFrame#stepRow[first="true"] {{ border-top: 0; }}
            QFrame#stepRow[state="now"] {{ background: {c["sel"]}; }}
            QLabel#stepGroup {{ color: {c["muted"]}; font-size: 8.5pt; }}
            QLabel#stepText {{ color: {c["text"]}; font-size: 10.5pt; }}
            QFrame#stepRow[state="now"] QLabel#stepText {{ font-weight: 700; color: {c["sel_text"]}; }}
            QFrame#stepRow[state="done"] QLabel#stepText {{ color: {c["muted"]}; }}
            QFrame#rowList {{
                background: transparent; border: 1px solid {c["border"]}; border-radius: 10px;
            }}
            QLabel#rowMain {{ color: {c["text"]}; font-size: 10.5pt; font-weight: 600; }}
            QLabel#rowBadge {{
                background: {c["ok_tint"]}; color: {c["ok_text"]}; border-radius: 9px;
                padding: 1px 8px; font-size: 8.5pt; font-weight: 700;
            }}
            QLabel#rowBadge[bad="true"] {{ background: {c["crit_tint"]}; color: {c["danger"]}; }}
            QPushButton#typeChoice {{ text-align: left; padding: 9px 12px; }}
            QPushButton#typeChoice:checked {{ border: 1.5px solid {c["text"]}; font-weight: 700; }}
            QFrame#tile {{ border: 1px dashed {c["border"]}; border-radius: 8px; }}
            QLabel#tileName {{ color: {c["muted"]}; font-size: 8.5pt; }}
            QLabel#tileValue {{ color: {c["text"]}; font-size: 13pt; font-weight: 700; }}
            QLabel#tileNote {{ color: {c["muted"]}; font-size: 8pt; }}
            QFrame#tile[bad="true"] {{
                border: 1px solid {c["danger"]}; background: {c["crit_tint"]};
            }}
            QFrame#tile[bad="true"] QLabel#tileValue, QFrame#tile[bad="true"] QLabel#tileNote {{
                color: {c["danger"]};
            }}
            QPushButton#feedTab {{
                background: transparent; border: 1px solid transparent; border-radius: 8px;
                color: {c["muted"]}; padding: 7px; font-weight: 700;
            }}
            QPushButton#feedTab:checked {{ background: {c["panel"]}; border-color: {c["border"]}; color: {c["text"]}; }}
            QLabel#msgAct {{ color: {c["text"]}; font-weight: 700; }}
            QLabel#msgText {{ color: {c["text"]}; }}
            QLabel#msgTime, QLabel#msgEmpty {{ color: {c["muted"]}; font-size: 8.5pt; }}
            QLabel#verifyLabel {{ background: {c["ok_tint"]}; color: {c["ok_text"]}; border-radius: 8px; padding: 8px 10px; }}
            QLabel#verifyLabel[bad="true"] {{ background: {c["crit_tint"]}; color: {c["danger"]}; }}
            QFrame#logEntry {{ background: transparent; border: 0; border-left: 2px solid {c["border"]}; }}
            QFrame#logEntry[bad="true"] {{ background: {c["crit_tint"]}; border-left-color: {c["danger"]}; }}
            QLabel#logHash {{ color: {c["muted"]}; font-family: Consolas, monospace; font-size: 8pt; }}
            QLabel#signedLabel {{ color: {c["ok_text"]}; }}
            QLabel#decisionTag {{
                background: {c["sel"]}; color: {c["sel_text"]}; border-radius: 6px; padding: 2px 8px;
                font-weight: 700;
            }}
            QLabel#decisionTitle {{ font-size: 13pt; font-weight: 800; color: {c["text"]}; }}
            QFrame#decisionRec {{ background: {c["panel2"]}; border: 1px solid {c["border"]}; border-radius: 8px; }}
            QFrame#decisionRec QLabel, QDialog#decisionDialog QLabel {{ background: transparent; }}
            QDialog#decisionDialog {{ background: {c["panel"]}; }}
            QPushButton#decisionOption {{ text-align: left; padding: 10px 14px; }}
            QPushButton#decisionOption[recommended="true"] {{
                border: 2px solid {c["accent"]}; background: {c["sel"]}; color: {c["sel_text"]}; font-weight: 700;
            }}
            QLabel#aiToast {{
                background: {c["text"]}; color: {c["panel"]}; border-radius: 8px; padding: 10px 16px;
            }}
            QLabel#aiAgentsTitle {{ color: {c["text"]}; font-weight: 600; }}
            QLabel#aiAgentsNote, QLabel#aiAgentsMessage {{ color: {c["muted"]}; }}
            QLabel#runsDrawerHint {{
                color: {c["muted"]}; background: {c["panel"]}; font-size: 9pt; padding: 8px 12px;
                border-top: 1px solid {c["border2"]};
            }}
            QPushButton#tabAdd {{
                background: transparent; border: none; border-radius: 0;
                color: {c["accent"]}; font-size: 16pt; font-weight: 400; padding: 0;
            }}
            QPushButton#tabAdd:hover {{ color: {c["accent"]}; background: {c["hover"]}; }}
            QFrame#exportCard {{
                background: {c["panel2"]}; border: 1px solid {c["border"]}; border-radius: 8px;
            }}
            QFrame#exportCard QLabel, QFrame#exportCard QRadioButton {{ background: transparent; }}
            QPushButton#exportChip {{ padding: 4px 10px; }}
            QPushButton#exportChip:checked {{
                background: {c["accent2"]}; color: {c["atext"]}; border-color: {c["accent2"]};
            }}
            QLabel#liveNotice {{ color: {c["accent"]}; background: transparent; }}
            QLabel#liveNotice[error="true"] {{ color: {c["danger"]}; }}
            QFrame#sideRail, QFrame#sidePanel {{
                background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 12px;
            }}
            QFrame#sideRail QWidget, QFrame#sideBar {{ background: transparent; }}
            QFrame#sideRail QFrame#cardDivider {{ background: {c["border2"]}; border: none; }}
            QFrame#sidePanel QScrollArea#sidePanelScroll,
            QFrame#sidePanel QScrollArea#sidePanelScroll > QWidget > QWidget,
            QFrame#sidePanel QStackedWidget, QFrame#sidePanel QLabel,
            QFrame#sidePanel QCheckBox, QWidget#sidePage {{ background: transparent; }}
            QLabel#sidePanelTitle {{ font-size: 15pt; font-weight: 800; color: {c["text"]}; }}
            QToolButton#railButton {{
                background: transparent; border: none; border-radius: 10px;
                color: {c["muted"]}; padding: 5px 0 4px 0; font-size: 8pt;
            }}
            QToolButton#railButton:hover {{ background: {c["hover"]}; }}
            QToolButton#railButton:checked {{
                background: {c["sel"]}; color: {c["sel_text"]}; font-weight: bold;
            }}
            QToolButton#panelClose {{ background: {c["hover"]}; border: none; border-radius: 8px; }}
            QToolButton#panelClose:hover {{ background: {c["sel"]}; }}
            QPushButton#drawerSection {{
                background: transparent; border: none; padding: 6px 0; min-height: 30px;
            }}
            QPushButton#drawerSection:hover {{ background: transparent; }}
            QLabel#drawerSectionTitle {{
                font-weight: 800; font-size: 11pt; color: {c["text"]}; background: transparent;
            }}
            QLabel[fieldLabel="true"] {{
                color: {c["muted"]}; font-size: 9pt; background: transparent; padding-top: 4px;
            }}
            QPushButton[softSegment="true"] {{
                background: {c["panel"]}; color: {c["text"]}; border: 1px solid {c["border"]};
                border-radius: 0; padding: 6px 8px; font-weight: 500;
            }}
            QPushButton[softSegment="true"][segmentFirst="true"] {{
                border-top-left-radius: 8px; border-bottom-left-radius: 8px;
            }}
            QPushButton[softSegment="true"][segmentLast="true"] {{
                border-top-right-radius: 8px; border-bottom-right-radius: 8px;
            }}
            QPushButton[softSegment="true"]:checked {{
                background: {c["sel"]}; color: {c["sel_text"]}; font-weight: bold;
            }}
            QPushButton[link="true"] {{
                background: transparent; border: none; color: {c["accent"]};
                font-weight: 700; padding: 6px 2px; text-align: left;
            }}
            QPushButton[link="true"]:hover {{ text-decoration: underline; background: transparent; }}
            QLabel#infoTitle {{ font-weight: 800; font-size: 11pt; background: transparent; }}
            QWidget#channelRow {{ background: transparent; }}
            QCalendarWidget QWidget {{ alternate-background-color: {c["panel2"]}; }}
            QCalendarWidget QAbstractItemView {{
                background: {c["panel"]}; selection-background-color: {c["accent2"]};
                selection-color: {c["atext"]};
            }}
            QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {c["panel"]}; }}
            QCalendarWidget QToolButton {{ background: transparent; color: {c["text"]}; border: none; }}
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
            QPushButton#primaryButton:hover {{
                background: {c["accent_hover"]}; border-color: {c["accent_hover"]};
            }}
            QPushButton#primaryButton:pressed {{
                background: {c["accent_pressed"]}; border-color: {c["accent_pressed"]};
            }}
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
            QMenu::item:selected {{ background: {c["sel"]}; color: {c["sel_text"]}; }}
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
                selection-color: {c["sel_text"]};
            }}
            QListWidget::item {{
                min-height: 28px; padding: 6px 8px; border-radius: 6px; margin: 1px;
            }}
            QListWidget::item:hover {{ background: {c["hover"]}; }}
            QListWidget::item:selected {{ background: {c["sel"]}; color: {c["sel_text"]}; }}
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
            QPushButton#tabClose:hover {{ background: {c["danger"]}; color: #ffffff; }}
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
            QWidget#analysisPage, QWidget#analysisContent {{ background: {c["bg"]}; }}
            QLabel#analysisTitle {{
                font-size: 13pt; font-weight: 800; color: {c["text"]}; background: transparent;
            }}
            QLabel#analysisMeta {{ color: {c["muted"]}; background: transparent; }}
            QPushButton#analysisTool, QPushButton#analysisMore {{ padding: 3px 10px; }}
            QPushButton#analysisMore {{ padding: 3px 0; }}
            QPushButton#analysisTool:checked {{
                background: {c["accent2"]}; color: {c["atext"]}; border-color: {c["accent2"]};
            }}
            QFrame#analysisChartCard {{
                background: {c["panel"]};
                border: 1px solid {c["border"]};
                border-radius: 10px;
            }}
            QWidget#analysisOptionGroup, QWidget#analysisOptionGroup > QLabel {{
                background: transparent;
            }}
            QTabWidget#flatTabs::pane {{ border: 0; border-top: 1px solid {c["border"]}; top: -1px; }}
            QTabWidget#flatTabs QTabBar::tab {{
                background: transparent; color: {c["muted"]};
                border: 0; border-bottom: 2px solid transparent;
                padding: 8px 14px; font-weight: 700;
            }}
            QTabWidget#flatTabs QTabBar::tab:hover {{ color: {c["text"]}; }}
            QTabWidget#flatTabs QTabBar::tab:selected {{
                color: {c["text"]}; border-bottom-color: {c["accent"]};
            }}
            QLabel#kpiName {{
                color: {c["muted"]}; font-size: 9pt; font-weight: 700; background: transparent;
            }}
            QLabel#kpiValue {{
                color: {c["text"]}; font-size: 14pt; font-weight: 800; background: transparent;
            }}
            QFrame#ruleRow {{
                background: {c["panel2"]};
                border: 1px solid {c["border2"]};
                border-radius: 7px;
            }}
            QLabel#monitorPlaceholder {{
                color: {c["muted"]}; font-size: 13pt; background: transparent;
            }}
            QPushButton#chatBubble {{
                background: {c["accent"]}; border: none; border-radius: 28px;
            }}
            QPushButton#chatBubble:hover {{ background: {c["accent_hover"]}; }}
            QFrame#chatPanel {{
                background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 12px;
            }}
            QFrame#chatHeader {{
                background: {c["accent"]}; border-top-left-radius: 12px; border-top-right-radius: 12px;
            }}
            QLabel#chatTitle {{ color: #ffffff; font-weight: 600; font-size: 11pt; background: transparent; }}
            QLabel#chatSubtitle {{ color: {HMI["cobalt_tint"]}; font-size: 8pt; background: transparent; }}
            QPushButton#chatHeaderButton {{
                background: transparent; color: #ffffff; border: none; padding: 4px 8px;
            }}
            QPushButton#chatHeaderButton:hover {{ background: rgba(255, 255, 255, 40); border-radius: 6px; }}
            QScrollArea#chatScroll, QWidget#chatBody {{ background: {c["panel"]}; }}
            QFrame#chatFooter {{ background: {c["panel"]}; border-top: 1px solid {c["border"]};
                border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; }}
            QLabel#chatUserMsg {{
                background: {c["accent"]}; color: #ffffff; padding: 8px 11px; border-radius: 10px;
            }}
            QLabel#chatAgentMsg {{
                background: {c["hover"]}; color: {c["text"]}; padding: 8px 11px; border-radius: 10px;
                border: 1px solid {c["border"]};
            }}
            QLabel#chatStepsMsg {{
                background: transparent; color: {c["muted"]}; padding: 4px 6px; font-size: 8.5pt;
                border-left: 2px solid {c["border"]};
            }}
            QLabel#chatNoticeMsg {{
                background: transparent; color: {c["text"]}; padding: 4px 6px; font-size: 8.5pt;
                border-left: 2px solid {c["accent"]};
            }}
            QLabel#chatErrorMsg {{
                background: transparent; color: {c["danger"]}; padding: 8px 11px; border-radius: 10px;
                border: 1px solid {c["danger"]};
            }}
            QWidget#controllerPage, QWidget#stepperColumn {{ background: {c["bg"]}; }}
            QLabel[muted="true"] {{ color: {c["muted"]}; background: transparent; }}
            QFrame#pageCard {{
                background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 10px;
            }}
            QFrame#pageCard QLabel, QFrame#pageCard QCheckBox {{ background: transparent; }}
            QFrame#stepCard {{
                background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 10px;
            }}
            QFrame#stepCard[state="open"] {{ border: 2px solid {c["accent"]}; }}
            QFrame#stepCard QWidget#stepBody, QFrame#stepCard QFrame#stepHead,
            QFrame#stepCard QLabel, QFrame#stepCard QCheckBox {{ background: transparent; }}
            QLabel#stepNumber {{
                background: {c["panel3"]}; color: {c["muted"]}; border-radius: 13px; font-weight: 800;
            }}
            QFrame#stepCard[state="open"] QLabel#stepNumber {{ background: {c["accent2"]}; color: #ffffff; }}
            QFrame#stepCard[state="done"] QLabel#stepNumber {{ background: {c["ok"]}; color: #ffffff; }}
            QFrame#stepCard[state="todo"] QLabel#stepTitle {{ color: {c["muted"]}; }}
            QFrame#intervalCard {{
                background: {c["panel2"]}; border: 1px solid {c["border"]}; border-radius: 6px;
            }}
            QFrame#intervalCard[open="true"] {{ border: 2px solid {c["accent"]}; }}
            QFrame#intervalCard QWidget, QFrame#intervalCard QFrame#intervalHead {{ background: transparent; }}
            QFrame#intervalCard QLineEdit, QFrame#intervalCard QComboBox {{ background: {c["panel"]}; }}
            QLabel#intervalNumber {{
                background: {c["panel3"]}; color: {c["muted"]}; border-radius: 4px; font-weight: 800;
            }}
            QFrame#intervalCard[open="true"] QLabel#intervalNumber {{ background: {c["accent2"]}; color: #ffffff; }}
            QPushButton#startButton {{ background: {c["ok"]}; color: #ffffff; border-color: {c["ok"]}; }}
            QPushButton#startButton:hover {{ background: {c["ok"]}; border-color: {c["ok"]}; }}
            QPushButton#stopButton {{
                background: {c["danger"]}; color: #ffffff; border-color: {c["danger"]};
            }}
            QPushButton#stopButton:hover {{
                background: {c["danger_hover"]}; border-color: {c["danger_hover"]};
            }}
            QPushButton#startButton:disabled, QPushButton#stopButton:disabled {{
                color: {c["muted"]}; background: {c["panel2"]}; border-color: {c["border2"]};
            }}
            QPushButton#dangerButton {{ color: {c["danger"]}; }}
            QLabel#runStatus, QLabel#runBar {{ background: transparent; }}
        """
        self.setStyleSheet(css)
        self.editor_area.update_theme(self.dark)
        self.controller_widget.update_theme(self.dark)
        self.ai_agents_widget.apply_theme(self.dark, c["bg"])
        self._rebuild_nav_icons(c)


def _app_version():
    try:
        from importlib.metadata import version

        return version("deepvac-insight")
    except Exception:
        return ""
