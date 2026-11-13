"""Tests of the PLC gate protocol."""

import struct

import pytest

from app.services import plc_gate as pg

pytestmark = pytest.mark.unit

CONTROL_PARAMS = [
    {"name": "duration", "type": 256},
    {"name": "temp", "type": 0},
    {"name": "flags", "type": 257},
]


def test_crc16_is_ccitt_false():
    assert pg.crc16(b"123456789") == 0x29B1


def test_package_layout_and_crc_check():
    body = pg.encode_text_cmd("ka")
    packet = pg.make_package(body)
    assert packet[:4] == bytes([0xAA, 0x55, 0xBB, 0x77])
    assert struct.unpack_from("<H", packet, 4)[0] == len(body)
    assert packet[6 : 6 + len(body)] == body
    assert pg.crc16(packet[4:]) == 0


def test_reader_splits_joins_and_resynchronises():
    a = pg.make_package(b"\x06")
    b = pg.make_package(pg.encode_text_cmd("get_states"))
    corrupt = bytearray(pg.make_package(b"\x07\x02hi"))
    corrupt[-1] ^= 0xFF
    reader = pg.PacketReader()
    stream = b"\x00junk" + a + bytes(corrupt) + b
    bodies = []
    for i in range(0, len(stream), 3):
        bodies += reader.feed(stream[i : i + 3])
    assert bodies == [b"\x06", pg.encode_text_cmd("get_states")]
    assert reader.crc_errors == 1


def test_confirm_commands_use_the_plc_spelling():
    assert pg.encode_text_cmd("confirm_alarm") == bytes([pg.OUT_TEXT_CMD]) + pg.pascal_string(
        "confrim_alarm"
    )
    assert pg.encode_text_cmd("stop") == b"\x01\x04stop"


def test_settings_map_is_sorted_like_qmap():
    body = pg.encode_settings_map({"b": 2.0, "a": 1.0})
    assert body == b"\x02\x02" + b"\x01a" + struct.pack("<f", 1.0) + b"\x01b" + struct.pack(
        "<f", 2.0
    )


def test_job_layout():
    intervals = [{"duration": 600, "temp": 30.0, "flags": 3 | (1 << pg.AWAIT_BIT)}]
    body = pg.encode_job(
        pg.PROG_TYPE_SETPOINT, intervals, CONTROL_PARAMS, {"count": 2, "start": 1, "end": 3}
    )
    expected = (
        bytes([pg.OUT_JOB])
        + struct.pack("<BHHHHH", 1, 0, 2, 1, 3, 1)
        + struct.pack("<IfI", 600, 30.0, 259)
    )
    assert body == expected


def test_manual_setpoint_intervals():
    hold_only = pg.manual_setpoint_intervals(30.0, 24.0, 0b011, hold_s=600)
    assert hold_only == [{"duration": 600, "temp": 30.0, "flags": 0b011 | (1 << pg.AWAIT_BIT)}]
    until_stopped = pg.manual_setpoint_intervals(30.0, 24.0, 0b011)
    assert until_stopped[0]["duration"] == pg.LONG_TIME_S and until_stopped[0]["flags"] == 0b011
    ramped = pg.manual_setpoint_intervals(30.0, 24.0, 0b011, ramp_rate_per_min=2.0)
    assert ramped[0] == {"duration": 180, "temp": 30.0, "flags": 0b011 | (1 << pg.RAMP_BIT)}
    with pytest.raises(ValueError):
        pg.manual_setpoint_intervals(30.0, None, 0b011, ramp_rate_per_min=2.0)


def test_decode_incoming_messages():
    names = bytes([pg.IN_STATE_NAMES, 2]) + pg.pascal_string("temp") + pg.pascal_string("temp_ref")
    assert pg.decode_message(names) == (pg.IN_STATE_NAMES, ["temp", "temp_ref"])
    values = bytes([pg.IN_STATE_VALUES, 2]) + struct.pack("<2f", 24.5, 30.0)
    assert pg.decode_message(values) == (pg.IN_STATE_VALUES, [24.5, 30.0])
    settings = (
        bytes([pg.IN_SETTINGS_MAP, 1]) + pg.pascal_string("teh_duty") + struct.pack("<f", 0.5)
    )
    assert pg.decode_message(settings) == (pg.IN_SETTINGS_MAP, {"teh_duty": 0.5})
    assert pg.decode_message(bytes([pg.IN_ALARM]) + struct.pack("<I", 1 << 9)) == (pg.IN_ALARM, 512)
    with pytest.raises(ValueError):
        pg.decode_message(bytes([pg.IN_STATE_VALUES, 3]) + struct.pack("<f", 1.0))
