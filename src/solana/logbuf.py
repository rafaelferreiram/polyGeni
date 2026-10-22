"""
In-memory live log of every bot (real account, simulation, lab bots) for the dashboard's log console.
Sources: each agent step (scout, strategy, AI, risk, executor…), every decision written to a ledger, cycle summaries
and warnings from the solana.* loggers. Ring buffer only — the ledgers remain the durable record.
"""
import itertools
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone

_buf: deque = deque(maxlen=3000)
_seq = itertools.count(1)
_lock = threading.Lock()


def label(path: str) -> str:
    """Ledger file -> which bot/account wrote the line."""
    name = os.path.basename(path or "").removesuffix(".db")
    if name.startswith("live_"):
        return "real" if name == "live_mainnet" else f"real-{name[5:]}"
    if name == "shadow":
        return "sim"
    if name.startswith("arena_"):
        return "lab:" + {"rules": "são-marcos", "trend": "endrick", "trend4h": "gabriel-jesus", "whales": "evair", "whales2": "dudu", "copy": "veiga", "short": "gustavo-gómez",
                         "trend4h_15m": "gabriel-jesus-15m", "whales_15m": "evair-15m", "copy_15m": "veiga-15m",
                         "claude-haiku-5-5": "valdivia"}.get(name[6:], name[6:].replace("claude-", ""))
    if name == "paper":
        return "solana-legado"
    return name or "sistema"


def add(source: str, level: str, msg: str):
    with _lock:
        _buf.append({"seq": next(_seq), "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "source": source, "level": level, "msg": str(msg)[:400]})


def since(after: int = 0, limit: int = 400) -> list[dict]:
    with _lock:
        rows = [r for r in _buf if r["seq"] > after]
    return rows[-limit:]


DECISION_LEVEL = {"entry": "TRADE", "exit": "TRADE", "veto": "WARN", "error": "ERROR", "kill_switch": "ERROR",
                  "reconcile": "WARN", "sleep": "INFO", "wake": "INFO"}


class _Handler(logging.Handler):
    """Warnings/errors and cycle summaries from the solana.* loggers."""

    def emit(self, record):
        if record.levelno >= logging.WARNING or "cycle" in record.getMessage():
            add(record.name.replace("solana.", ""), "ERROR" if record.levelno >= logging.ERROR
                else "WARN" if record.levelno >= logging.WARNING else "INFO", record.getMessage())


def install():
    lg = logging.getLogger("solana")
    if not any(isinstance(h, _Handler) for h in lg.handlers):
        lg.addHandler(_Handler(level=logging.INFO))
