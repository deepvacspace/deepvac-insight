"""LicenseActivationWindow — browser-based device-code activation gate shown before login."""

from app.device_code_window import DeviceCodeWindow
from app.services import licensing_client


class LicenseActivationWindow(DeviceCodeWindow):
    def __init__(self):
        self.activated_license = None
        self.activated_account = None
        super().__init__(quit_app_on_close=True)

    def _window_title(self):
        return self.tr("Activate — Deepvac Insight")

    def _heading_text(self):
        return self.tr("Activate this installation")

    def _start_flow(self):
        return licensing_client.start_activation()

    def _poll_flow_status(self, flow):
        return licensing_client.poll_activation_status(flow.activation_id)

    def _complete_flow(self, flow):
        license_payload, account_payload = licensing_client.complete_activation(flow.activation_id)
        self.activated_license = license_payload
        self.activated_account = account_payload
        self.close()
