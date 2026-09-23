"""ProfileDialog — change display name, email, and password for the signed-in user."""

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.account_link_window import AccountLinkWindow
from app.services import auth_service, licensing_client


class ProfileDialog(QDialog):
    def __init__(self, user, parent=None):
        super().__init__(parent)
        self.user = dict(user)
        self.updated_user = dict(user)
        self.setWindowTitle(self.tr("Profile"))
        self.setMinimumWidth(360)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(14)

        self.org_membership_lbl = QLabel()
        self.org_membership_lbl.setObjectName("orgMembershipLabel")
        self.org_membership_lbl.setVisible(False)
        root.addWidget(self.org_membership_lbl)

        info_lbl = QLabel(self.tr("ACCOUNT DETAILS"))
        info_lbl.setObjectName("sectionLabel")
        root.addWidget(info_lbl)

        form = QFormLayout()
        self.name_ed = QLineEdit(self.user["name"])
        self.email_ed = QLineEdit(self.user["email"])
        form.addRow(self.tr("Name"), self.name_ed)
        form.addRow(self.tr("Email"), self.email_ed)
        root.addLayout(form)

        save_info_btn = QPushButton(self.tr("Save Changes"))
        save_info_btn.setObjectName("primaryButton")
        save_info_btn.clicked.connect(self._save_profile)
        root.addWidget(save_info_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        root.addWidget(sep)

        pw_lbl = QLabel(self.tr("CHANGE PASSWORD"))
        pw_lbl.setObjectName("sectionLabel")
        root.addWidget(pw_lbl)

        pw_form = QFormLayout()
        self.current_pw_ed = QLineEdit()
        self.current_pw_ed.setEchoMode(QLineEdit.Password)
        self.new_pw_ed = QLineEdit()
        self.new_pw_ed.setEchoMode(QLineEdit.Password)
        self.confirm_pw_ed = QLineEdit()
        self.confirm_pw_ed.setEchoMode(QLineEdit.Password)
        pw_form.addRow(self.tr("Current password"), self.current_pw_ed)
        pw_form.addRow(self.tr("New password"), self.new_pw_ed)
        pw_form.addRow(self.tr("Confirm new password"), self.confirm_pw_ed)
        root.addLayout(pw_form)

        save_pw_btn = QPushButton(self.tr("Update Password"))
        save_pw_btn.clicked.connect(self._save_password)
        root.addWidget(save_pw_btn)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        root.addWidget(sep2)

        hub_lbl = QLabel(self.tr("DEEPVAC HUB ACCOUNT"))
        hub_lbl.setObjectName("sectionLabel")
        root.addWidget(hub_lbl)

        self.hub_status_lbl = QLabel()
        self.hub_status_lbl.setWordWrap(True)
        root.addWidget(self.hub_status_lbl)

        self.hub_action_btn = QPushButton()
        self.hub_action_btn.clicked.connect(self._on_hub_action)
        root.addWidget(self.hub_action_btn)
        self._refresh_hub_section()

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    def _save_profile(self):
        try:
            updated = auth_service.update_profile(
                self.user["id"],
                name=self.name_ed.text().strip(),
                email=self.email_ed.text().strip(),
            )
        except auth_service.AuthError as exc:
            QMessageBox.warning(self, self.tr("Profile"), str(exc))
            return
        self.updated_user = updated
        self.user = updated
        QMessageBox.information(self, self.tr("Profile"), self.tr("Profile updated."))

    def _save_password(self):
        new_pw = self.new_pw_ed.text()
        if new_pw != self.confirm_pw_ed.text():
            QMessageBox.warning(self, self.tr("Profile"), self.tr("New passwords do not match."))
            return
        try:
            auth_service.change_password(self.user["id"], self.current_pw_ed.text(), new_pw)
        except auth_service.AuthError as exc:
            QMessageBox.warning(self, self.tr("Profile"), str(exc))
            return
        self.current_pw_ed.clear()
        self.new_pw_ed.clear()
        self.confirm_pw_ed.clear()
        QMessageBox.information(self, self.tr("Profile"), self.tr("Password updated."))

    def _refresh_hub_section(self):
        org_name = self.user.get("hub_org_name") if self.user.get("hub_user_id") else None
        self.org_membership_lbl.setText(org_name or "")
        self.org_membership_lbl.setVisible(bool(org_name))
        if self.user.get("hub_user_id"):
            self.hub_status_lbl.setText(
                self.tr("Linked to {0} ({1}).").format(
                    self.user.get("hub_email") or "?", self.user.get("hub_org_name") or "?"
                )
            )
            self.hub_action_btn.setText(self.tr("Unlink"))
        else:
            self.hub_status_lbl.setText(self.tr("Not linked to a Deepvac Hub account yet."))
            self.hub_action_btn.setText(self.tr("Link my account…"))

    def _on_hub_action(self):
        if self.user.get("hub_user_id"):
            self._unlink_account()
        else:
            self._link_account()

    def _link_account(self):
        organization_id = licensing_client.current_organization_id()
        if not organization_id:
            QMessageBox.warning(
                self,
                self.tr("Link Account"),
                self.tr("No license found for this installation yet."),
            )
            return

        window = AccountLinkWindow(organization_id, parent=self)
        window.exec()
        if not window.linked_account:
            return
        payload = window.linked_account
        try:
            updated = auth_service.link_hub_account(
                self.user["id"],
                hub_user_id=payload["user_id"],
                hub_email=payload["email"],
                hub_org_id=payload["organization_id"],
                hub_org_name=payload["organization_name"],
            )
        except auth_service.AuthError as exc:
            QMessageBox.warning(self, self.tr("Link Account"), str(exc))
            return
        self.user = updated
        self.updated_user = updated
        self._refresh_hub_section()
        QMessageBox.information(self, self.tr("Link Account"), self.tr("Account linked."))

    def _unlink_account(self):
        try:
            updated = auth_service.unlink_hub_account(self.user["id"])
        except auth_service.AuthError as exc:
            QMessageBox.warning(self, self.tr("Link Account"), str(exc))
            return
        self.user = updated
        self.updated_user = updated
        self._refresh_hub_section()
