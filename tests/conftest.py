import json
from pathlib import Path

import pytest

from cableguard.config import collector_config, load_config


@pytest.fixture
def ccfg(tmp_path: Path):
    """Collector config writing into a temporary private folder."""
    cfg = load_config()
    cfg["paths"]["private"] = str(tmp_path / "private")
    cfg["collector"]["flush_interval_s"] = 3600
    cfg["collector"]["stats_interval_s"] = 3600
    cfg["collector"]["backoff_initial_s"] = 0.01
    cfg["collector"]["backoff_max_s"] = 0.02
    return collector_config(cfg, root=tmp_path)


def position_msg(mmsi=237000001, lat=37.9, lon=23.6, sog=0.4, status=1,
                 time_utc="2026-11-13 10:15:30.123456789 +0000 UTC",
                 mtype="PositionReport") -> bytes:
    body = {"UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": 123.4,
            "TrueHeading": 120, "NavigationalStatus": status, "RateOfTurn": 0,
            "Timestamp": 30, "PositionAccuracy": True, "Raim": False, "Valid": True}
    if mtype != "PositionReport":
        body.pop("NavigationalStatus")
        body.pop("RateOfTurn")
    return json.dumps({
        "MessageType": mtype,
        "MetaData": {"MMSI": mmsi, "ShipName": "TEST", "latitude": lat, "longitude": lon,
                     "time_utc": time_utc},
        "Message": {mtype: body},
    }).encode()


def static_msg(mmsi=237000001) -> bytes:
    return json.dumps({
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": mmsi, "time_utc": "2026-11-13 10:16:00 +0000 UTC"},
        "Message": {"ShipStaticData": {
            "UserID": mmsi, "Name": "TEST SHIP@@@@", "CallSign": "SV1AB  ",
            "ImoNumber": 0, "Type": 70, "Dimension": {"A": 100, "B": 20, "C": 10, "D": 12},
            "Eta": {"Month": 11, "Day": 14, "Hour": 6, "Minute": 0},
            "MaximumStaticDraught": 7.5, "Destination": "PIRAEUS@@", "Valid": True}},
    }).encode()
