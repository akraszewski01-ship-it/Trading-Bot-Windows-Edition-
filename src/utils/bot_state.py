"""
Shared in-process state store that the trading engine writes to and the
dashboard server reads from. Thread-safe singleton updated every cycle.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List


class BotState:
    _lock = threading.Lock()
    _state: Dict[str, Any] = {
        "status": "starting",
        "mode": "paper",
        "ts": "",
        "portfolio": {"value": 0.0, "daily_pnl": 0.0, "daily_pnl_pct": 0.0, "win_rate": 0.0, "trades": 0},
        "captain": {"regime": "-", "kelly_mult": 0.0, "min_edge": 0.0, "halt": False, "reasoning": "Waiting for first review...", "source": "-"},
        "forecasts": {},
        "positions": [],
        "quotes": {},
        "spot": {},
        "recent_decisions": [],
        "circuit_breaker": {"halted": False, "reason": ""},
    }
    _log_lines: List[str] = []
    _max_log = 500

    @classmethod
    def update(cls, patch: Dict[str, Any]) -> None:
        with cls._lock:
            cls._state.update(patch)
            cls._state["ts"] = datetime.now(timezone.utc).isoformat()

    @classmethod
    def get(cls) -> Dict[str, Any]:
        with cls._lock:
            return dict(cls._state)

    @classmethod
    def push_log(cls, line: str) -> None:
        with cls._lock:
            cls._log_lines.append(line)
            if len(cls._log_lines) > cls._max_log:
                cls._log_lines = cls._log_lines[-cls._max_log:]

    @classmethod
    def get_logs(cls, last_n: int = 200) -> List[str]:
        with cls._lock:
            return cls._log_lines[-last_n:]

    @classmethod
    def push_decision(cls, decision: Dict[str, Any]) -> None:
        with cls._lock:
            cls._state["recent_decisions"].insert(0, decision)
            cls._state["recent_decisions"] = cls._state["recent_decisions"][:50]


# Module-level singleton
state = BotState()
