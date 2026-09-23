"""LoginWindow — sign in / create account screen shown before the main window."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.common import ICON_PATH, LOGO_PATH
from app.services import auth_service


class LoginWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.authenticated_user = None
        self.setWindowTitle(self.tr("Sign in — Deepvac Insight"))
        self.setWindowIcon(QIcon(ICON_PATH))
        self.setFixedSize(420, 560)
        self._build_ui()
        self._apply_style()
        self._show_login()
        self._center_on_screen()

    # ── UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 32, 36, 28)
        root.setSpacing(14)

        logo_lbl = QLabel()
        pix = QPixmap(LOGO_PATH)
        if not pix.isNull():
            logo_lbl.setPixmap(pix.scaledToHeight(48, Qt.SmoothTransformation))
        else:
            logo_lbl.setText(self.tr("DEEPVAC"))
            logo_lbl.setObjectName("brand")
        logo_lbl.setAlignment(Qt.AlignCenter)
        root.addWidget(logo_lbl)

        self.title_lbl = QLabel(self.tr("Sign in"))
        self.title_lbl.setObjectName("formTitle")
        self.title_lbl.setAlignment(Qt.AlignCenter)
        root.addWidget(self.title_lbl)

        self.error_lbl = QLabel("")
        self.error_lbl.setObjectName("errorLabel")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setVisible(False)
        root.addWidget(self.error_lbl)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_login_page())
        self.stack.addWidget(self._build_signup_page())
        root.addWidget(self.stack)
        root.addStretch(1)

    def _field(self, placeholder, password=False):
        ed = QLineEdit()
        ed.setPlaceholderText(placeholder)
        ed.setMinimumHeight(34)
        if password:
            ed.setEchoMode(QLineEdit.Password)
        return ed

    def _build_login_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.login_email = self._field(self.tr("Email"))
        self.login_password = self._field(self.tr("Password"), password=True)
        self.login_email.returnPressed.connect(self._do_login)
        self.login_password.returnPressed.connect(self._do_login)
        lay.addWidget(self.login_email)
        lay.addWidget(self.login_password)

        self.remember_cb = QCheckBox(self.tr("Remember me on this device"))
        lay.addWidget(self.remember_cb)

        login_btn = QPushButton(self.tr("Sign In"))
        login_btn.setObjectName("primaryButton")
        login_btn.setMinimumHeight(36)
        login_btn.clicked.connect(self._do_login)
        lay.addWidget(login_btn)

        switch_row = QHBoxLayout()
        switch_lbl = QLabel(self.tr("Don't have an account?"))
        switch_lbl.setObjectName("mutedLabel")
        switch_btn = QPushButton(self.tr("Create one"))
        switch_btn.setObjectName("linkButton")
        switch_btn.setCursor(Qt.PointingHandCursor)
        switch_btn.clicked.connect(lambda: self._show_signup())
        switch_row.addWidget(switch_lbl)
        switch_row.addWidget(switch_btn)
        switch_row.addStretch(1)
        lay.addLayout(switch_row)
        lay.addStretch(1)
        return page

    def _build_signup_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.signup_name = self._field(self.tr("Full name"))
        self.signup_email = self._field(self.tr("Email"))
        self.signup_password = self._field(self.tr("Password (min. 8 characters)"), password=True)
        self.signup_confirm = self._field(self.tr("Confirm password"), password=True)
        self.signup_confirm.returnPressed.connect(self._do_signup)
        for w in [self.signup_name, self.signup_email, self.signup_password, self.signup_confirm]:
            lay.addWidget(w)

        signup_btn = QPushButton(self.tr("Create Account"))
        signup_btn.setObjectName("primaryButton")
        signup_btn.setMinimumHeight(36)
        signup_btn.clicked.connect(self._do_signup)
        lay.addWidget(signup_btn)

        self.signup_switch_row = QHBoxLayout()
        switch_lbl = QLabel(self.tr("Already have an account?"))
        switch_lbl.setObjectName("mutedLabel")
        switch_btn = QPushButton(self.tr("Sign in"))
        switch_btn.setObjectName("linkButton")
        switch_btn.setCursor(Qt.PointingHandCursor)
        switch_btn.clicked.connect(self._show_login)
        self.signup_switch_row.addWidget(switch_lbl)
        self.signup_switch_row.addWidget(switch_btn)
        self.signup_switch_row.addStretch(1)
        lay.addLayout(self.signup_switch_row)
        lay.addStretch(1)
        return page

    def _show_login(self):
        self._clear_error()
        self.title_lbl.setText(self.tr("Sign in"))
        self.stack.setCurrentIndex(0)
        self.login_email.setFocus()

    def _show_signup(self):
        self._clear_error()
        self.title_lbl.setText(self.tr("Create account"))
        self.stack.setCurrentIndex(1)
        self.signup_name.setFocus()

    def _center_on_screen(self):
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - self.width()) // 2,
            geo.y() + (geo.height() - self.height()) // 2,
        )

    # ── Actions ──────────────────────────────────────────────────────────

    def _do_login(self):
        email = self.login_email.text().strip()
        password = self.login_password.text()
        if not email or not password:
            self._show_error(self.tr("Enter your email and password."))
            return
        user = auth_service.authenticate(email, password)
        if not user:
            self._show_error(self.tr("Incorrect email or password."))
            return
        if self.remember_cb.isChecked():
            self._remember(user)
        self._finish(user)

    def _do_signup(self):
        name = self.signup_name.text().strip()
        email = self.signup_email.text().strip()
        password = self.signup_password.text()
        confirm = self.signup_confirm.text()
        if password != confirm:
            self._show_error(self.tr("Passwords do not match."))
            return
        try:
            user = auth_service.create_user(name, email, password)
        except auth_service.AuthError as exc:
            self._show_error(str(exc))
            return
        self._finish(user)

    def _remember(self, user):
        from PySide6.QtCore import QSettings

        token = auth_service.set_remember_token(user["id"])
        QSettings("DeepVac", "Insight").setValue("auth/remember_token", token)

    def _finish(self, user):
        self.authenticated_user = user
        self.close()

    def _show_error(self, msg):
        self.error_lbl.setText(msg)
        self.error_lbl.setVisible(True)

    def _clear_error(self):
        self.error_lbl.setVisible(False)
        self.error_lbl.setText("")

    def closeEvent(self, event):
        QApplication.instance().quit()
        super().closeEvent(event)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { background: #0b1020; color: #f8fafc; font-family: "Segoe UI"; font-size: 10.5pt; }
            QLabel#brand { font-size: 20px; font-weight: 900; color: #60a5fa; }
            QLabel#formTitle { font-size: 16px; font-weight: 800; color: #f8fafc; margin-top: 4px; }
            QLabel#mutedLabel { color: #94a3b8; background: transparent; }
            QLabel#errorLabel {
                color: #ff6f7d; background: rgba(255,111,125,0.08);
                border: 1px solid #ff6f7d; border-radius: 6px; padding: 6px 10px;
            }
            QLineEdit {
                background: #111827; color: #f8fafc; border: 1px solid #253247;
                border-radius: 8px; padding: 6px 10px;
            }
            QLineEdit:focus { border-color: #60a5fa; }
            QCheckBox { color: #94a3b8; spacing: 7px; background: transparent; }
            QPushButton {
                background: #1e293b; color: #f8fafc; border: 1px solid #253247;
                border-radius: 8px; padding: 6px 12px; font-weight: 650;
            }
            QPushButton:hover { background: #172033; border-color: #60a5fa; }
            QPushButton#primaryButton { background: #2563eb; border-color: #2563eb; color: #ffffff; }
            QPushButton#primaryButton:hover { background: #60a5fa; border-color: #60a5fa; }
            QPushButton#linkButton {
                background: transparent; border: none; color: #60a5fa; font-weight: 700; padding: 0;
            }
            QPushButton#linkButton:hover { color: #93c5fd; }
        """)
