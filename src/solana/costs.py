"""
Real AI cost bookkeeping. Every Claude API call is priced from the usage the API returns (tokens, cache,
web searches) and appended to costs.db. A monthly budget stops all AI calls once spent; the rules bot keeps
working for free. Prices: platform.claude.com/docs/en/about-claude/pricing (verified 2026-10-21).
"""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from src.solana import config as cfg

# $ per million tokens: (input, output). Haiku's >100K-token tier is ignored: our prompts are a few K tokens.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
CACHE_READ_MULT = {"claude-fable-5-1": 0.025, "claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05}
SEARCH_USD = 10.0 / 1000


class BudgetExceeded(RuntimeError):
    pass


def price(model: str, usage: dict) -> float:
    p_in, p_out = PRICES.get(model, (10.0, 50.0))          # unknown model: assume the most expensive
    read_mult = CACHE_READ_MULT.get(model, 0.1)
    return (usage.get("input_tokens", 0) * p_in + usage.get("cache_creation", 0) * p_in * 1.25
            + usage.get("cache_read", 0) * p_in * read_mult + usage.get("output_tokens", 0) * p_out) / 1e6 \
        + usage.get("searches", 0) * SEARCH_USD


class CostBook:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS costs (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, model TEXT,
                who TEXT, input_tokens INTEGER, output_tokens INTEGER, searches INTEGER, usd REAL NOT NULL);
            CREATE TRIGGER IF NOT EXISTS costs_no_update BEFORE UPDATE ON costs
            BEGIN SELECT RAISE(ABORT, 'cost book is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS costs_no_delete BEFORE DELETE ON costs
            BEGIN SELECT RAISE(ABORT, 'cost book is append-only'); END;""")

    def record(self, model: str, who: str, usage: dict) -> float:
        usd = price(model, usage)
        with self._lock:
            self.conn.execute("INSERT INTO costs (ts,model,who,input_tokens,output_tokens,searches,usd) VALUES (?,?,?,?,?,?,?)",
                              (datetime.now(timezone.utc).isoformat(timespec="seconds"), model, who,
                               usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("searches", 0), usd))
        return usd

    def total(self, days: float | None = None, model: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(usd),0) FROM costs WHERE 1=1", []
        if days is not None:
            sql += " AND ts >= ?"
            args.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds"))
        if model:
            sql += " AND model = ?"
            args.append(model)
        with self._lock:
            return self.conn.execute(sql, args).fetchone()[0]

    def month_spent(self) -> float:
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            return self.conn.execute("SELECT COALESCE(SUM(usd),0) FROM costs WHERE ts >= ?",
                                     (start.isoformat(timespec="seconds"),)).fetchone()[0]

    def calls(self, days: float = 30) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM costs WHERE ts >= ?", (since,)).fetchone()[0]


_book: CostBook | None = None


def book() -> CostBook:
    global _book
    if _book is None:
        _book = CostBook(cfg.COSTS_DB_PATH)
    return _book


def check_budget():
    spent = book().month_spent()
    if spent >= cfg.LLM_MONTHLY_BUDGET_USD:
        raise BudgetExceeded(f"orçamento de IA do mês esgotado (${spent:.2f} de ${cfg.LLM_MONTHLY_BUDGET_USD:.2f})")
