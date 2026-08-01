"""ChamberSession -- one live chamber connection plus everything that used
to be scattered across DeepVacDesktop/mixin instance attributes for it:
sample buffering, reconnect bookkeeping, this chamber's alarm rules and
their runtime evaluation state, and the manual-setpoint/test-profile
step-sequencer.

The app supports up to five of these connected at once (see
main_window.py's _connect_chamber()/_chamber_sessions), one per tab in
Live Monitoring. Each session owns its own QTimers so a running manual
setpoint or test profile keeps advancing even while the Controller page
has a *different* chamber selected -- "independent per chamber" means the
sequencing state lives here, not on whatever widget happens to be visible.

app/services/tcp_client.ChamberConnection itself is unchanged -- it was
already a clean per-connection socket wrapper; this just gives each
instance of it a home with the bookkeeping that used to assume there was
only ever one.
"""

import time
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QTimer, Signal

from app.services import alarms_service
from app.services.tcp_client import ChamberConnection

# Reconnect attempts after a connection drop the user didn't request
# themselves, with linear backoff -- moved as-is from the old
# views/monitoring.py module constants.
_RECONNECT_MAX_ATTEMPTS = 5
_RECONNECT_DELAY_MS = 3000

# How many chambers can have an open Live Monitoring tab (and therefore a
# ChamberSession) at once -- enforced by main_window.py's _connect_chamber().
MAX_CONNECTED_CHAMBERS = 5


class ChamberSession(QObject):
    connected = Signal()
    disconnected = Signal()
    connection_error = Signal(str)
    sample_received = Signal(dict)  # re-emitted after buffering + alarm eval
    reconnecting = Signal(int, int)  # attempt, max_attempts
    reconnect_failed = Signal()
    alarms_changed = Signal()
    manual_state_changed = Signal()
    test_state_changed = Signal()

    def __init__(self, chamber: dict, parent=None):
        super().__init__(parent)
        self.chamber = chamber  # {id, name, host, port}
        self.tcp = ChamberConnection(self)

        self.mon_buffer = []  # every sample this session -- see clear_buffer()/Save as Run
        self.last_seen = None
        self.user_disconnected = False
        self.reconnect_attempts = 0
        self._reconnect_timer = None

        self.alarms = []  # this chamber's rules, each with runtime-only _active/etc keys
        self.session_test_profile_name = None  # last test profile run this session, if any

        self.manual_running = False
        self.manual_started_at = None
        self.last_manual_stop_error = None
        self.last_manual_elapsed_s = None
        self._manual_timer = None

        self.test_running_profile = None
        self.test_step_index = None
        self.test_step_started_at = None
        self.last_test_profile_name = None
        self.last_test_finished = False
        self.last_test_stop_error = None
        self._test_timer = None
        self._test_tick_timer = None

        self.tcp.connected.connect(self._on_connected)
        self.tcp.disconnected.connect(self._on_disconnected)
        self.tcp.connection_error.connect(self._on_error)
        self.tcp.sample_received.connect(self._on_sample)

        self._load_alarm_rules()

    # ── Connection lifecycle ─────────────────────────────────────────────────

    def connect(self):
        self.user_disconnected = False
        self.reconnect_attempts = 0
        self.tcp.connect_to_host(self.chamber["host"], self.chamber["port"])

    def disconnect(self):
        self.user_disconnected = True  # don't auto-reconnect after this
        if self._reconnect_timer is not None:
            self._reconnect_timer.stop()
        self.tcp.disconnect_from_host()

    def teardown(self):
        """Called when this session is being permanently removed (its tab
        closed) -- stops every timer and the socket without scheduling a
        reconnect. Safe to call regardless of connection state."""
        self.user_disconnected = True
        if self._reconnect_timer is not None:
            self._reconnect_timer.stop()
        if self._manual_timer is not None:
            self._manual_timer.stop()
        if self._test_timer is not None:
            self._test_timer.stop()
        if self._test_tick_timer is not None:
            self._test_tick_timer.stop()
        if self.tcp.is_connected():
            self.tcp.disconnect_from_host()

    def is_connected(self):
        return self.tcp.is_connected()

    def _on_connected(self):
        self.reconnect_attempts = 0
        self.connected.emit()

    def _on_disconnected(self):
        if self.test_running_profile is not None:
            self.stop_test(error="chamber disconnected")
        if self.manual_running:
            self.stop_manual_setpoint(error="chamber disconnected")
        for alarm in self.alarms:
            alarm["_active"] = False
        self.alarms_changed.emit()
        self.disconnected.emit()
        self._maybe_schedule_reconnect()

    def _on_error(self, msg):
        self.connection_error.emit(msg)

    def _maybe_schedule_reconnect(self):
        if self.user_disconnected:
            return
        if self.reconnect_attempts >= _RECONNECT_MAX_ATTEMPTS:
            self.reconnect_failed.emit()
            return
        self.reconnect_attempts += 1
        self.reconnecting.emit(self.reconnect_attempts, _RECONNECT_MAX_ATTEMPTS)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(
            lambda: self.tcp.connect_to_host(self.chamber["host"], self.chamber["port"])
        )
        self._reconnect_timer.start(_RECONNECT_DELAY_MS * self.reconnect_attempts)

    # ── Samples ──────────────────────────────────────────────────────────────

    def _on_sample(self, sample):
        self.last_seen = datetime.now(timezone.utc)
        self.mon_buffer.append(sample)
        self._evaluate_alarms(sample)
        self.sample_received.emit(sample)

    def clear_buffer(self):
        self.mon_buffer = []
        self.session_test_profile_name = None

    # ── Alarms (per-chamber) ─────────────────────────────────────────────────

    def _load_alarm_rules(self):
        try:
            rules = alarms_service.list_rules(chamber_id=self.chamber["id"])
        except Exception:
            rules = []
        for rule in rules:
            rule["_active"] = False
            rule["_last_value"] = None
            rule["_condition_since"] = None
            rule["_event_id"] = None
        self.alarms = rules

    def add_alarm_rule(
        self,
        name,
        variable,
        condition,
        value,
        value2,
        severity,
        deadband=0.0,
        delay_s=0.0,
        created_by="Unknown",
    ):
        rule = alarms_service.add_rule(
            name,
            variable,
            condition,
            value,
            value2,
            severity,
            deadband=deadband,
            delay_s=delay_s,
            created_by=created_by,
            chamber_id=self.chamber["id"],
            chamber_name=self.chamber["name"],
        )
        rule["_active"] = False
        rule["_last_value"] = None
        rule["_condition_since"] = None
        rule["_event_id"] = None
        self.alarms.append(rule)
        self.alarms_changed.emit()
        return rule

    def delete_alarm_rule(self, idx):
        if 0 <= idx < len(self.alarms):
            rule = self.alarms.pop(idx)
            alarms_service.delete_rule(rule["id"])
        self.alarms_changed.emit()

    def _evaluate_alarms(self, sample):
        changed = False
        now = time.monotonic()
        for alarm in self.alarms:
            value = sample.get(alarm["variable"])
            alarm["_last_value"] = value
            if not isinstance(value, (int, float)):
                continue

            cond = alarm["condition"]
            deadband = alarm.get("deadband") or 0.0
            threshold, threshold2 = alarm["value"], alarm.get("value2")

            if cond == "above":
                raw_active = value > threshold
                clears = value <= threshold - deadband
            elif cond == "below":
                raw_active = value < threshold
                clears = value >= threshold + deadband
            elif cond == "outside range" and threshold2 is not None:
                lo, hi = min(threshold, threshold2), max(threshold, threshold2)
                raw_active = value < lo or value > hi
                clears = (lo + deadband) <= value <= (hi - deadband)
            else:
                continue

            if not alarm["_active"]:
                if raw_active:
                    if alarm.get("_condition_since") is None:
                        alarm["_condition_since"] = now
                    if now - alarm["_condition_since"] >= (alarm.get("delay_s") or 0.0):
                        alarm["_active"] = True
                        changed = True
                        alarm["_event_id"] = alarms_service.record_trigger(alarm, value)
                else:
                    alarm["_condition_since"] = None
            elif clears:
                alarm["_active"] = False
                alarm["_condition_since"] = None
                changed = True
                alarms_service.record_clear(alarm.get("_event_id"))
                alarm["_event_id"] = None
        if changed:
            self.alarms_changed.emit()

    # ── Manual setpoint ──────────────────────────────────────────────────────

    def start_manual_setpoint(self, temp, pressure):
        if not self.is_connected():
            raise RuntimeError("Connect to a chamber first.")
        if self.test_running_profile is not None:
            raise RuntimeError("Stop the running test profile first.")
        payload = {
            "cmd": "set_point",
            "temperature": temp,
            "pressure": pressure,
            "step_index": None,
            "step_label": "Manual setpoint",
            "profile_name": None,
        }
        self.tcp.send_command(payload)

        self.manual_running = True
        self.manual_started_at = time.monotonic()
        self.last_manual_stop_error = None
        self.last_manual_elapsed_s = None
        self._manual_timer = QTimer(self)
        self._manual_timer.timeout.connect(self.manual_state_changed.emit)
        self._manual_timer.start(1000)
        self.manual_state_changed.emit()

    def stop_manual_setpoint(self, error=None):
        if not self.manual_running:
            return
        if self._manual_timer is not None:
            self._manual_timer.stop()
            self._manual_timer = None
        elapsed = time.monotonic() - self.manual_started_at if self.manual_started_at else 0.0
        self.manual_running = False
        self.manual_started_at = None
        self.last_manual_stop_error = error
        self.last_manual_elapsed_s = elapsed
        self.manual_state_changed.emit()

    # ── Test profiles ────────────────────────────────────────────────────────

    def start_test(self, profile):
        if not self.is_connected():
            raise RuntimeError("Connect to a chamber first.")
        if self.manual_running:
            raise RuntimeError("Stop the manual setpoint first.")
        if not profile or not profile.get("steps"):
            return
        self.test_running_profile = profile
        self.session_test_profile_name = profile["name"]
        self.last_test_profile_name = profile["name"]
        self.test_step_index = 0
        self._test_send_current_step()

    def stop_test(self, error=None):
        if self.test_running_profile is None:
            return
        self.last_test_finished = False
        self.last_test_stop_error = error
        self._test_cleanup()

    def _test_send_current_step(self):
        profile = self.test_running_profile
        step = profile["steps"][self.test_step_index]
        payload = {
            "cmd": "set_point",
            "temperature": step["setpoint_temp"],
            "pressure": step["setpoint_pressure"],
            "step_index": self.test_step_index,
            "step_label": step["label"],
            "profile_name": profile["name"],
        }
        try:
            self.tcp.send_command(payload)
        except RuntimeError as exc:
            self.stop_test(error=str(exc))
            return

        self.test_step_started_at = time.monotonic()
        self.test_state_changed.emit()

        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.timeout.connect(self._test_advance_step)
        self._test_timer.start(max(0, int(step["duration_s"] * 1000)))

        if self._test_tick_timer is None:
            self._test_tick_timer = QTimer(self)
            self._test_tick_timer.timeout.connect(self.test_state_changed.emit)
        self._test_tick_timer.start(1000)

    def _test_advance_step(self):
        if self.test_running_profile is None:
            return
        self.test_step_index += 1
        if self.test_step_index >= len(self.test_running_profile["steps"]):
            self._test_finish()
            return
        self._test_send_current_step()

    def _test_finish(self):
        self.last_test_finished = True
        self.last_test_stop_error = None
        self._test_cleanup()

    def _test_cleanup(self):
        if self._test_timer is not None:
            self._test_timer.stop()
            self._test_timer = None
        if self._test_tick_timer is not None:
            self._test_tick_timer.stop()
        self.test_running_profile = None
        self.test_step_index = None
        self.test_step_started_at = None
        self.test_state_changed.emit()
