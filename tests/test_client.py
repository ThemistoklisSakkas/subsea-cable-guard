import asyncio
import json
import random
from contextlib import asynccontextmanager

import polars as pl
import pytest

from cableguard.collector.client import (
    Collector,
    SingleInstance,
    backoff_delay,
    run,
    subscription,
)
from cableguard.collector.status import day_summary, last_heartbeat
from tests.conftest import position_msg, static_msg


def test_backoff_grows_and_is_capped():
    rng = random.Random(0)
    delays = [backoff_delay(a, 2, 300, rng) for a in range(12)]
    assert 1 <= delays[0] <= 2
    assert all(d <= 300 for d in delays)
    assert delays[-1] >= 150                     # capped base 300 -> at least half


def test_subscription_contains_bbox_and_types(ccfg):
    sub = json.loads(subscription(ccfg, "KEY"))
    assert sub["APIKey"] == "KEY"
    assert sub["BoundingBoxes"] == [ccfg.bbox.as_aisstream()]
    assert "PositionReport" in sub["FilterMessageTypes"]


def test_single_instance_lock():
    with SingleInstance(47699):
        with pytest.raises(RuntimeError, match="another collector"):
            with SingleInstance(47699):
                pass
    with SingleInstance(47699):                  # released after exit
        pass


def test_collector_counts_parse_errors(ccfg):
    col = Collector(ccfg)
    assert col.handle(b"garbage") is None
    assert col.handle(position_msg()) == "position"
    assert col.stats.parse_errors == 1
    assert col.stats.positions == 1


class FakeWS:
    """Plays a script of messages; then either drops the connection or idles."""

    def __init__(self, messages, then="drop"):
        self.messages = list(messages)
        self.then = then
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.then == "drop":
            raise OSError("connection reset")
        await asyncio.sleep(3600)


def fake_connect(sessions, stop):
    calls = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        calls.append(kwargs)
        if not sessions:
            stop.set()
            raise OSError("no more sessions")
        yield sessions.pop(0)

    connect.calls = calls
    return connect


async def test_run_reconnects_writes_and_logs(ccfg):
    stop = asyncio.Event()
    s1 = FakeWS([position_msg(mmsi=1), static_msg(mmsi=1)], then="drop")
    s2 = FakeWS([position_msg(mmsi=2, time_utc="2026-11-13 10:20:00 +0000 UTC")], then="drop")
    connect = fake_connect([s1, s2], stop)

    col = await asyncio.wait_for(run(ccfg, "KEY", stop=stop, connect=connect), timeout=10)

    assert len(connect.calls) == 3              # 2 sessions + 1 failed attempt
    assert connect.calls[0]["compression"] == "deflate"
    assert s1.sent[0]["APIKey"] == "KEY"        # subscribed on every connect
    assert s2.sent[0]["APIKey"] == "KEY"
    assert col.stats.positions == 2 and col.stats.static == 1
    assert col.stats.connects == 2

    summary = day_summary(ccfg.out_dir)
    pos = summary.filter(pl.col("kind") == "positions")
    assert pos["rows"].sum() == 2
    hb = last_heartbeat(ccfg.log_dir)
    assert hb["event"] == "stop" and hb["rows_written"] == 3


async def test_idle_watchdog_triggers_reconnect(ccfg):
    from dataclasses import replace
    cfg = replace(ccfg, idle_timeout_s=0.2)
    stop = asyncio.Event()
    s1 = FakeWS([position_msg()], then="idle")  # goes silent -> watchdog
    connect = fake_connect([s1], stop)
    col = await asyncio.wait_for(run(cfg, "KEY", stop=stop, connect=connect), timeout=10)
    assert len(connect.calls) == 2
    assert col.stats.positions == 1


async def test_duration_stops_cleanly(ccfg):
    stop = asyncio.Event()
    s1 = FakeWS([position_msg()], then="idle")
    connect = fake_connect([s1], stop)
    col = await asyncio.wait_for(
        run(ccfg, "KEY", stop=stop, connect=connect, duration_s=0.5), timeout=10)
    assert col.stats.rows_written == 1          # final flush on stop
