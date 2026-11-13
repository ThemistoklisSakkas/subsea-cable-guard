"""aisstream.io websocket client with reconnect, watchdog and periodic flushing."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import websockets

from cableguard.collector.compact import compact_before
from cableguard.collector.messages import (
    POSITION_SCHEMA,
    STATIC_SCHEMA,
    AisstreamError,
    parse_message,
)
from cableguard.collector.stats import Stats, append_jsonl
from cableguard.collector.writer import DailyBuffer
from cableguard.config import CollectorConfig

log = logging.getLogger("cableguard.collector")


def backoff_delay(attempt: int, initial: float, maximum: float,
                  rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter: base doubles per failed attempt, capped.

    The jitter (random half of the base) stops many clients - or one client
    restarted by a scheduler - from hammering the server in lock-step.
    """
    rng = rng or random.Random()
    base = min(maximum, initial * (2 ** max(attempt, 0)))
    return base / 2 + rng.uniform(0, base / 2)


def subscription(cfg: CollectorConfig, api_key: str) -> str:
    return json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [cfg.bbox.as_aisstream()],
        "FilterMessageTypes": list(cfg.message_types),
    })


class SingleInstance:
    """Refuse to start a second collector (it would write duplicate data).

    Binding a localhost port is a cross-platform lock that the OS releases
    automatically if the process dies - no stale lock files.
    """

    def __init__(self, port: int):
        self.port = port
        self._sock: socket.socket | None = None

    def __enter__(self) -> SingleInstance:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no port sharing
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", self.port))
        except OSError as exc:
            s.close()
            raise RuntimeError(
                f"another collector seems to be running (port {self.port} in use)") from exc
        self._sock = s
        return self

    def __exit__(self, *exc: object) -> None:
        if self._sock:
            self._sock.close()


class Collector:
    """Holds buffers and stats; independent of the network so it can be unit-tested."""

    def __init__(self, cfg: CollectorConfig):
        self.cfg = cfg
        self.stats = Stats()
        self.positions = DailyBuffer(cfg.out_dir / "positions", POSITION_SCHEMA)
        self.static = DailyBuffer(cfg.out_dir / "static", STATIC_SCHEMA)
        self.stats_path = cfg.log_dir / "stats.jsonl"

    def handle(self, raw: bytes | str, recv_ts: datetime | None = None) -> str | None:
        recv_ts = recv_ts or datetime.now(UTC)
        try:
            parsed = parse_message(raw, recv_ts)
        except ValueError:
            self.stats.parse_errors += 1
            return None
        if parsed is None:
            self.stats.on_message(_peek_type(raw), None)
            return None
        kind, row = parsed
        self.stats.on_message(row["msg_type"], kind)
        (self.positions if kind == "position" else self.static).add(row)
        return kind

    def needs_flush(self) -> bool:
        return len(self.positions) + len(self.static) >= self.cfg.flush_max_rows

    def flush(self) -> int:
        n = self.positions.flush() + self.static.flush()
        self.stats.rows_written += n
        return n

    def compact(self, today=None) -> None:
        today = today or datetime.now(UTC).date()
        for kind_dir in (self.positions.kind_dir, self.static.kind_dir):
            compact_before(kind_dir, today)

    def heartbeat(self, event: str = "heartbeat") -> dict:
        snap = self.stats.snapshot(event)
        append_jsonl(self.stats_path, snap)
        log.info("%s: up %.0fs connected %.1f%% msgs/period %d pos %d static %d written %d "
                 "reconnects %d", event, snap["uptime_s"], 100 * snap["connected_frac"],
                 snap["messages_since_last"], snap["positions"], snap["static"],
                 snap["rows_written"], snap["reconnects"])
        return snap


def _peek_type(raw: bytes | str) -> str | None:
    try:
        return json.loads(raw).get("MessageType")
    except Exception:
        return None


async def _housekeeping(col: Collector, stop: asyncio.Event) -> None:
    """Flush on a timer, write heartbeats, compact finished days at UTC rollover."""
    loop = asyncio.get_running_loop()
    last_flush = last_stats = loop.time()
    last_day = datetime.now(UTC).date()
    while not stop.is_set():
        await asyncio.sleep(1)
        now = loop.time()
        if now - last_flush >= col.cfg.flush_interval_s or col.needs_flush():
            col.flush()
            last_flush = now
        if now - last_stats >= col.cfg.stats_interval_s:
            col.heartbeat()
            last_stats = now
        today = datetime.now(UTC).date()
        if today != last_day:
            col.flush()  # yesterday's tail must be on disk before compaction
            await asyncio.to_thread(col.compact, today)
            last_day = today


async def run(cfg: CollectorConfig, api_key: str, *,
              stop: asyncio.Event | None = None,
              connect: Callable[..., Any] = websockets.connect,
              duration_s: float | None = None) -> Collector:
    """Run until `stop` is set, `duration_s` elapses, or the task is cancelled."""
    stop = stop or asyncio.Event()
    col = Collector(cfg)
    col.compact()                       # finish any day left over from a previous run
    col.heartbeat("start")
    house = asyncio.create_task(_housekeeping(col, stop))
    if duration_s:
        asyncio.get_running_loop().call_later(duration_s, stop.set)
    attempt = 0
    try:
        while not stop.is_set():
            session_msgs = 0
            try:
                async with connect(cfg.url, compression="deflate", open_timeout=20,
                                   ping_interval=20, ping_timeout=20,
                                   max_size=2 ** 22) as ws:
                    await asyncio.wait_for(ws.send(subscription(cfg, api_key)),
                                           cfg.subscribe_timeout_s)
                    col.stats.on_connect()
                    log.info("connected and subscribed (bbox %s)", cfg.bbox)
                    while not stop.is_set():
                        recv = asyncio.ensure_future(ws.recv())
                        stopper = asyncio.ensure_future(stop.wait())
                        done, _ = await asyncio.wait({recv, stopper},
                                                     timeout=cfg.idle_timeout_s,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        stopper.cancel()
                        if recv not in done:
                            recv.cancel()
                            if stop.is_set():
                                break
                            raise TimeoutError(f"no message for {cfg.idle_timeout_s:.0f} s")
                        col.handle(recv.result())
                        session_msgs += 1
                        attempt = 0     # data is flowing: reset the backoff
            except AisstreamError as exc:
                log.error("server error: %s", exc)
            except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("connection lost: %s: %s", type(exc).__name__, exc)
            finally:
                col.stats.on_disconnect()
            if stop.is_set():
                break
            if session_msgs == 0:
                # aisstream drops an invalid key silently instead of sending an error.
                log.warning("connection closed before any data arrived - "
                            "if this repeats, check AISSTREAM_API_KEY in .env")
            delay = backoff_delay(attempt, cfg.backoff_initial_s, cfg.backoff_max_s)
            attempt += 1
            log.info("reconnecting in %.1f s (attempt %d)", delay, attempt)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        stop.set()
        house.cancel()
        col.flush()
        col.heartbeat("stop")
    return col


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = RotatingFileHandler(log_dir / "collector.log", maxBytes=5_000_000,
                                 backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_h]
    if sys.stderr is not None:          # pythonw.exe (background task) has no console
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        handlers.append(console)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("websockets").setLevel(logging.WARNING)
