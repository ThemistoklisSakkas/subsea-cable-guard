"""Self-monitoring: uptime, message counts, reconnects, written rows.

Every heartbeat is one JSON line in stats.jsonl, so data completeness can be
audited later (e.g. "the collector was disconnected 2.1% of the week").
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Stats:
    started_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_mono: float = field(default_factory=time.monotonic)
    by_type: Counter = field(default_factory=Counter)
    positions: int = 0
    static: int = 0
    ignored: int = 0
    parse_errors: int = 0
    rows_written: int = 0
    connects: int = 0
    disconnects: int = 0
    connected_s: float = 0.0
    _connected_since: float | None = None
    _window: Counter = field(default_factory=Counter)

    def on_connect(self) -> None:
        self.connects += 1
        self._connected_since = time.monotonic()

    def on_disconnect(self) -> None:
        if self._connected_since is not None:
            self.connected_s += time.monotonic() - self._connected_since
            self._connected_since = None
            self.disconnects += 1

    @property
    def is_connected(self) -> bool:
        return self._connected_since is not None

    def on_message(self, msg_type: str | None, kind: str | None) -> None:
        self.by_type[msg_type or "unknown"] += 1
        self._window["messages"] += 1
        if kind == "position":
            self.positions += 1
        elif kind == "static":
            self.static += 1
        else:
            self.ignored += 1

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_mono

    def total_connected_s(self) -> float:
        extra = time.monotonic() - self._connected_since if self._connected_since else 0.0
        return self.connected_s + extra

    def snapshot(self, event: str = "heartbeat") -> dict:
        up = self.uptime_s()
        conn = self.total_connected_s()
        snap = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            "uptime_s": round(up, 1),
            "connected_s": round(conn, 1),
            "connected_frac": round(conn / up, 4) if up > 0 else 0.0,
            "connected_now": self.is_connected,
            "connects": self.connects,
            "reconnects": max(self.connects - 1, 0),
            "messages_since_last": self._window["messages"],
            "positions": self.positions,
            "static": self.static,
            "ignored": self.ignored,
            "parse_errors": self.parse_errors,
            "rows_written": self.rows_written,
            "by_type": dict(self.by_type),
        }
        self._window.clear()
        return snap


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
