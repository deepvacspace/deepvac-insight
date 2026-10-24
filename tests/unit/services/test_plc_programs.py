"""Test profiles as PLC programs (app/services/plc_programs.py)."""

import pytest

from app.services import plc_gate, plc_programs

pytestmark = pytest.mark.unit

HOLD_FLAGS = 0b011 | (1 << plc_gate.AWAIT_BIT)
RAMP_FLAGS = 0b011 | (1 << plc_gate.RAMP_BIT)


def test_steps_become_go_to_and_hold_intervals():
    profile = {
        "name": "Soak",
        "program": None,
        "steps": [
            {"setpoint_temp": 40.0, "setpoint_pressure": None, "duration_s": 600.4},
            {"setpoint_temp": -10.0, "setpoint_pressure": None, "duration_s": 1200},
        ],
    }
    intervals, cycle = plc_programs.program_for_profile(profile, 0b011)
    assert intervals == [
        {"duration": 600, "temp": 40.0, "flags": HOLD_FLAGS},
        {"duration": 1200, "temp": -10.0, "flags": HOLD_FLAGS},
    ]
    assert cycle == {}
    assert [plc_programs.interval_mode(i) for i in intervals] == [plc_programs.HOLD] * 2


def test_an_imported_plc_program_runs_as_it_is():
    program = {
        "intervals": [{"duration": 3600, "temp": 60, "flags": RAMP_FLAGS}],
        "cycle": {"count": 3, "start": 1, "end": 1},
    }
    intervals, cycle = plc_programs.program_for_profile(
        {"name": "60 test", "program": program, "steps": []}, 0b011
    )
    assert intervals == program["intervals"] and cycle == program["cycle"]
    assert plc_programs.interval_mode(intervals[0]) == plc_programs.RAMP
    assert plc_programs.total_duration_s(intervals, cycle) == 3 * 3600


def test_pressure_only_steps_cannot_run_on_the_plc():
    profile = {
        "name": "Vacuum",
        "program": None,
        "steps": [{"setpoint_temp": None, "setpoint_pressure": 1e-3, "duration_s": 60}],
    }
    with pytest.raises(plc_programs.ProgramError, match="no temperature"):
        plc_programs.program_for_profile(profile, 0b011)


def test_limits():
    plc_programs.check_limits([{"temp": 20.0}], -75, 150)
    with pytest.raises(plc_programs.ProgramError, match="outside"):
        plc_programs.check_limits([{"temp": 20.0}, {"temp": 200.0}], -75, 150)
