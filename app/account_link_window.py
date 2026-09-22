"""AccountLinkWindow — browser-based device-code flow to link a local profile to a hub account."""

from app.device_code_window import DeviceCodeWindow
from app.services import licensing_client


class AccountLinkWindow(DeviceCodeWindow):
    def __init__(self, organization_id, parent=None):
        self._organization_id = organization_id
        self.linked_account = None
        super().__init__(quit_app_on_close=False, parent=parent)

    def _window_title(self):
        return self.tr("Link Account — DeepVac Insight")

    def _heading_text(self):
        return self.tr("Link your DeepVac Hub account")

    def _start_flow(self):
        return licensing_client.start_account_link(self._organization_id)

    def _poll_flow_status(self, flow):
        return licensing_client.poll_account_link_status(flow.link_id)

    def _complete_flow(self, flow):
        self.linked_account = licensing_client.complete_account_link(flow.link_id)
        self.close()
