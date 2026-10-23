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

    def total(self, days: float | None = None, model: str | None = None, who: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(usd),0) FROM costs WHERE 1=1", []
        if who:
            sql += " AND who = ?"
            args.append(who)
        if days is not None:
            sql += " AND ts >= ?"
            args.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds"))
        if model:
            sql += " AND model = ?"
            args.append(model)
        with self._lock:
            return self.conn.execute(sql, args).fetchone()[0]

    def between(self, start_iso: str, end_iso: str) -> tuple[float, int]:
        with self._lock:
            r = self.conn.execute("SELECT COALESCE(SUM(usd),0), COUNT(*) FROM costs WHERE ts >= ? AND ts < ?",
                                  (start_iso, end_iso)).fetchone()
        return r[0], r[1]

    def month_spent(self) -> float:
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            return self.conn.execute("SELECT COALESCE(SUM(usd),0) FROM costs WHERE ts >= ?",
                                     (start.isoformat(timespec="seconds"),)).fetchone()[0]

    def day_spent(self) -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            return self.conn.execute("SELECT COALESCE(SUM(usd),0) FROM costs WHERE ts >= ?",
                                     (start.isoformat(timespec="seconds"),)).fetchone()[0]

    def calls(self, days: float = 30) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM costs WHERE ts >= ?", (since,)).fetchone()[0]


    def breakdown(self, by: str, days: float | None = None) -> list[dict]:
        """Spend grouped by 'model', 'who' or 'day' (UTC date), biggest first (days: newest first)."""
        col = {"model": "model", "who": "who", "day": "substr(ts, 1, 10)"}[by]
        sql, args = f"SELECT {col}, COUNT(*), SUM(usd), SUM(input_tokens), SUM(output_tokens) FROM costs", []
        if days is not None:
            sql += " WHERE ts >= ?"
            args.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds"))
        sql += f" GROUP BY {col} ORDER BY " + ("1 DESC" if by == "day" else "3 DESC")
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [{"key": k, "calls": n, "usd": round(u or 0, 4), "in_tokens": i or 0, "out_tokens": o or 0} for k, n, u, i, o in rows]


_book: CostBook | None = None


def book() -> CostBook:
    global _book
    if _book is None:
        _book = CostBook(cfg.COSTS_DB_PATH)
    return _book


def real_profit_month() -> float:
    """Trading P&L of the real account since the 1st of this month (closed trades, fees and funding included)."""
    try:
        from src.solana import hl_live
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        return sum(t["pnl_usd"] for t in hl_live.get_live_engine().ledger.closed_trades() if t["exit_ts"] >= start)
    except Exception:
        return 0.0


def allowance() -> float:
    """What AI may spend this month: a small base to keep learning + a share of what trading earned, capped."""
    return min(cfg.LLM_MONTHLY_BUDGET_USD, cfg.AI_BASE_MONTHLY_USD + cfg.AI_PROFIT_SHARE * max(0.0, real_profit_month()))


def check_budget():
    spent, cap = book().month_spent(), allowance()
    if spent >= cap:
        raise BudgetExceeded(f"IA pausada: gastou ${spent:.2f} de ${cap:.2f} permitidos este mês "
                             f"(base ${cfg.AI_BASE_MONTHLY_USD:.2f} + {cfg.AI_PROFIT_SHARE:.0%} do lucro real)")
    if cfg.AI_DAILY_BUDGET_USD and (today := book().day_spent()) >= cfg.AI_DAILY_BUDGET_USD:
        raise BudgetExceeded(f"IA pausada até amanhã: gastou ${today:.2f} hoje, o teto diário é "
                             f"${cfg.AI_DAILY_BUDGET_USD:.2f} (um dia chegou a queimar 79% do mês)")
