import json
from datetime import UTC, datetime

import pytest

from cableguard.collector.messages import AisstreamError, parse_message, parse_time_utc
from tests.conftest import position_msg, static_msg

NOW = datetime(2026, 11, 13, 10, 15, 31, tzinfo=UTC)


def test_parse_go_timestamp_truncates_nanoseconds():
    t = parse_time_utc("2026-11-13 10:15:30.123456789 +0000 UTC")
    assert t == datetime(2026, 11, 13, 10, 15, 30, 123456, tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-11-13 10:15:30 +0000 UTC", "2026-11-13T10:15:30Z"])
def test_parse_timestamp_without_fraction(value):
    assert parse_time_utc(value) == datetime(2026, 11, 13, 10, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "yesterday", 123])
def test_parse_bad_timestamp_returns_none(value):
    assert parse_time_utc(value) is None


def test_class_a_position():
    kind, row = parse_message(position_msg(), NOW)
    assert kind == "position"
    assert row["mmsi"] == 237000001
    assert (row["lat"], row["lon"], row["sog"]) == (37.9, 23.6, 0.4)
    assert row["nav_status"] == 1          # at anchor
    assert row["recv_ts"] == NOW
    assert row["ts_utc"].microsecond == 123456


def test_class_b_position_has_no_nav_status():
    kind, row = parse_message(position_msg(mtype="StandardClassBPositionReport"), NOW)
    assert kind == "position"
    assert row["nav_status"] is None
    assert row["msg_type"] == "StandardClassBPositionReport"


def test_raw_values_are_not_cleaned():
    """Sentinels like heading 511 are kept: cleaning is a later, documented step."""
    msg = json.loads(position_msg())
    msg["Message"]["PositionReport"]["TrueHeading"] = 511
    _, row = parse_message(json.dumps(msg), NOW)
    assert row["heading"] == 511


def test_ship_static_data_strips_ais_padding():
    kind, row = parse_message(static_msg(), NOW)
    assert kind == "static"
    assert row["name"] == "TEST SHIP"
    assert row["callsign"] == "SV1AB"
    assert row["destination"] == "PIRAEUS"
    assert row["imo"] is None              # 0 means not available
    assert (row["dim_a"], row["dim_b"]) == (100, 20)
    assert row["ship_type"] == 70


def test_static_data_report_parts():
    base = {"MessageType": "StaticDataReport", "MetaData": {"MMSI": 1},
            "Message": {"StaticDataReport": {"UserID": 1, "Valid": True}}}
    base["Message"]["StaticDataReport"].update(
        PartNumber=False, ReportA={"Valid": True, "Name": "SMALL BOAT"})
    _, a = parse_message(json.dumps(base), NOW)
    assert (a["part"], a["name"]) == ("A", "SMALL BOAT")

    base["Message"]["StaticDataReport"].update(
        PartNumber=True, ReportB={"Valid": True, "ShipType": 37, "CallSign": "X",
                                  "Dimension": {"A": 5, "B": 5, "C": 2, "D": 2}})
    _, b = parse_message(json.dumps(base), NOW)
    assert (b["part"], b["ship_type"], b["dim_a"]) == ("B", 37, 5)


def test_unknown_type_is_ignored():
    raw = json.dumps({"MessageType": "SafetyBroadcastMessage", "MetaData": {},
                      "Message": {"SafetyBroadcastMessage": {"UserID": 1}}})
    assert parse_message(raw, NOW) is None


def test_position_without_coordinates_is_dropped():
    msg = json.loads(position_msg())
    del msg["Message"]["PositionReport"]["Latitude"]
    del msg["MetaData"]["latitude"]
    assert parse_message(json.dumps(msg), NOW) is None


def test_malformed_json_raises_value_error():
    with pytest.raises(ValueError):
        parse_message(b"{not json", NOW)


def test_server_error_object_raises():
    with pytest.raises(AisstreamError, match="Api Key"):
        parse_message(b'{"error": "Api Key Is Not Valid"}', NOW)
