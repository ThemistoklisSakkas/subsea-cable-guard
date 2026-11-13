"""Parse aisstream.io JSON messages into flat rows.

The collector stores messages *as received* (raw layer). It does not clean or
filter values such as heading=511 or lat=91 - that is the cleaning step's job,
so the raw archive stays a faithful record of what was broadcast.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import polars as pl

POSITION_TYPES = {
    "PositionReport",                 # Class A, msg 1-3
    "StandardClassBPositionReport",   # Class B, msg 18
    "ExtendedClassBPositionReport",   # Class B, msg 19
}
STATIC_TYPES = {
    "ShipStaticData",                 # Class A, msg 5
    "StaticDataReport",               # Class B, msg 24 (part A or B)
}

POSITION_SCHEMA: dict[str, pl.DataType] = {
    "recv_ts": pl.Datetime("us", "UTC"),   # when our collector received it
    "ts_utc": pl.Datetime("us", "UTC"),    # aisstream receive time (MetaData.time_utc)
    "mmsi": pl.Int64,
    "msg_type": pl.Utf8,
    "lat": pl.Float64,
    "lon": pl.Float64,
    "sog": pl.Float64,                     # knots
    "cog": pl.Float64,                     # degrees
    "heading": pl.Int32,                   # degrees, 511 = not available
    "nav_status": pl.Int32,                # Class A only
    "rot": pl.Float64,                     # Class A only
    "utc_second": pl.Int32,                # AIS 'Timestamp' field (second of minute)
    "pos_accuracy": pl.Boolean,
    "raim": pl.Boolean,
    "valid": pl.Boolean,
}

STATIC_SCHEMA: dict[str, pl.DataType] = {
    "recv_ts": pl.Datetime("us", "UTC"),
    "ts_utc": pl.Datetime("us", "UTC"),
    "mmsi": pl.Int64,
    "msg_type": pl.Utf8,
    "part": pl.Utf8,                       # "A"/"B" for StaticDataReport, else null
    "name": pl.Utf8,
    "callsign": pl.Utf8,
    "imo": pl.Int64,
    "ship_type": pl.Int32,
    "dim_a": pl.Int32,                     # metres from GNSS antenna to bow
    "dim_b": pl.Int32,                     # ... to stern
    "dim_c": pl.Int32,                     # ... to port
    "dim_d": pl.Int32,                     # ... to starboard
    "draught": pl.Float64,
    "destination": pl.Utf8,
    "eta_month": pl.Int32,
    "eta_day": pl.Int32,
    "eta_hour": pl.Int32,
    "eta_minute": pl.Int32,
    "valid": pl.Boolean,
}

# Go-style timestamp, e.g. "2023-04-04 12:46:10.553384643 +0000 UTC"
_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?")


def parse_time_utc(value: Any) -> datetime | None:
    """Parse aisstream's MetaData.time_utc. Fractions beyond microseconds are truncated."""
    if not isinstance(value, str):
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        return None
    day, hms, frac = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        dt = datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(microsecond=micro, tzinfo=UTC)


def _clean_str(value: Any) -> str | None:
    """AIS 6-bit text is padded with '@' and spaces; empty means not available."""
    if not isinstance(value, str):
        return None
    s = value.replace("@", " ").strip()
    return s or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _meta(meta: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in meta:
            return meta[k]
    return None


def _position_row(mtype: str, body: dict, meta: dict, recv_ts: datetime) -> dict | None:
    mmsi = _int(body.get("UserID")) or _int(_meta(meta, "MMSI"))
    lat = _float(body.get("Latitude"))
    lon = _float(body.get("Longitude"))
    if lat is None:
        lat = _float(_meta(meta, "latitude", "Latitude"))
    if lon is None:
        lon = _float(_meta(meta, "longitude", "Longitude"))
    if mmsi is None or lat is None or lon is None:
        return None
    return {
        "recv_ts": recv_ts,
        "ts_utc": parse_time_utc(_meta(meta, "time_utc", "TimeUtc")),
        "mmsi": mmsi,
        "msg_type": mtype,
        "lat": lat,
        "lon": lon,
        "sog": _float(body.get("Sog")),
        "cog": _float(body.get("Cog")),
        "heading": _int(body.get("TrueHeading")),
        "nav_status": _int(body.get("NavigationalStatus")),
        "rot": _float(body.get("RateOfTurn")),
        "utc_second": _int(body.get("Timestamp")),
        "pos_accuracy": _bool(body.get("PositionAccuracy")),
        "raim": _bool(body.get("Raim")),
        "valid": _bool(body.get("Valid")),
    }


def _static_row(mtype: str, body: dict, meta: dict, recv_ts: datetime) -> dict | None:
    mmsi = _int(body.get("UserID")) or _int(_meta(meta, "MMSI"))
    if mmsi is None:
        return None
    row: dict[str, Any] = {k: None for k in STATIC_SCHEMA}
    row.update(
        recv_ts=recv_ts,
        ts_utc=parse_time_utc(_meta(meta, "time_utc", "TimeUtc")),
        mmsi=mmsi,
        msg_type=mtype,
        valid=_bool(body.get("Valid")),
    )
    if mtype == "ShipStaticData":
        dim = body.get("Dimension") or {}
        eta = body.get("Eta") or {}
        row.update(
            name=_clean_str(body.get("Name")),
            callsign=_clean_str(body.get("CallSign")),
            imo=_int(body.get("ImoNumber")) or None,      # 0 = not available
            ship_type=_int(body.get("Type")),
            dim_a=_int(dim.get("A")), dim_b=_int(dim.get("B")),
            dim_c=_int(dim.get("C")), dim_d=_int(dim.get("D")),
            draught=_float(body.get("MaximumStaticDraught")),
            destination=_clean_str(body.get("Destination")),
            eta_month=_int(eta.get("Month")), eta_day=_int(eta.get("Day")),
            eta_hour=_int(eta.get("Hour")), eta_minute=_int(eta.get("Minute")),
        )
    else:  # StaticDataReport: PartNumber False -> part A (name), True -> part B (type, dims)
        is_b = body.get("PartNumber") is True
        row["part"] = "B" if is_b else "A"
        if is_b:
            rep = body.get("ReportB") or {}
            dim = rep.get("Dimension") or {}
            row.update(
                callsign=_clean_str(rep.get("CallSign")),
                ship_type=_int(rep.get("ShipType")),
                dim_a=_int(dim.get("A")), dim_b=_int(dim.get("B")),
                dim_c=_int(dim.get("C")), dim_d=_int(dim.get("D")),
            )
        else:
            rep = body.get("ReportA") or {}
            row["name"] = _clean_str(rep.get("Name"))
    return row


def parse_message(raw: bytes | str, recv_ts: datetime) -> tuple[str, dict] | None:
    """Return ("position" | "static", row) or None if the message is not usable.

    Raises ValueError for malformed JSON so the caller can count parse errors.
    Raises AisstreamError if the server sent an error object.
    """
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ValueError("message is not a JSON object")
    if "error" in msg:
        raise AisstreamError(str(msg["error"]))

    mtype = msg.get("MessageType")
    body = (msg.get("Message") or {}).get(mtype) if isinstance(mtype, str) else None
    meta = msg.get("MetaData") or {}
    if not isinstance(body, dict):
        return None
    if mtype in POSITION_TYPES:
        row = _position_row(mtype, body, meta, recv_ts)
        return ("position", row) if row else None
    if mtype in STATIC_TYPES:
        row = _static_row(mtype, body, meta, recv_ts)
        return ("static", row) if row else None
    return None


class AisstreamError(RuntimeError):
    """The server replied with an error object, e.g. an invalid API key."""
