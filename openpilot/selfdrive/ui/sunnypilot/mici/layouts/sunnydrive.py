import urllib.parse

from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigParamControl
from openpilot.selfdrive.ui.mici.widgets.dialog import BigConfirmationDialog
from openpilot.selfdrive.ui.mici.widgets.pairing_dialog import PairingDialog
from openpilot.sunnypilot.sunnydrive import pairing
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.scroller import NavScroller


class SunnydrivePairingDialog(PairingDialog):
  """One screen supplies both the QR secret and the six digit fallback code."""

  def __init__(self):
    self._state = pairing.open_window()
    super().__init__()
    code = self._state["code"]
    self._pair_label.set_text(f"scan with sunnydrive\nor enter {code[:3]} {code[3:]}")

  def _get_pairing_url(self):
    query = urllib.parse.urlencode({"device": pairing.device_id(), "secret": self._state["secret"]})
    return f"sunnydrive://pair?{query}"


class SunnydriveLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._pending_id = ""
    self._enable = BigParamControl("enabled", "SunnydriveEnabled")
    self._pair = BigButton("pair phone", "QR code or 6 digit code")
    self._pair.set_click_callback(lambda: gui_app.push_widget(SunnydrivePairingDialog()))
    self._approve = BigButton("approve phone", "waiting for a request")
    self._approve.set_click_callback(self._confirm_pending)
    self._unpair = BigButton("paired phones", "none")
    self._unpair.set_click_callback(self._confirm_unpair_all)
    self._scroller.add_widgets([self._enable, self._pair, self._approve, self._unpair])

  def _update_state(self):
    super()._update_state()
    self._enable.refresh()
    requests = pairing.pairing_requests()
    pending = next(((request_id, request) for request_id, request in reversed(list(requests.items())) if request.get("status") == "pending"), None)
    self._pending_id = pending[0] if pending else ""
    self._approve.set_value(pending[1].get("name", "Sunnydrive phone") if pending else "waiting for a request")
    self._approve.set_visible(bool(pending))
    count = len(pairing.paired_clients())
    self._unpair.set_value(f"{count} paired" if count else "none")
    self._unpair.set_visible(count > 0)

  def _confirm_pending(self):
    if not self._pending_id:
      return
    request_id = self._pending_id
    icon = gui_app.texture("icons_mici/settings/device/pair.png", 64, 64)
    gui_app.push_widget(BigConfirmationDialog("slide to pair phone", icon, lambda: pairing.approve_request(request_id)))

  def _confirm_unpair_all(self):
    icon = gui_app.texture("icons_mici/settings/network/new/trash.png", 54, 64)
    gui_app.push_widget(BigConfirmationDialog("slide to unpair all phones", icon, lambda: pairing.unpair(), red=True))
