"""
Paper ledger in its own SQLite file (never touches polygeni.db).

- fills, decisions: append-only (UPDATE/DELETE blocked by triggers); fills idempotent by order_id
- positions: materialized view of open positions, rebuilt from fills on reconcile
- state: small key/value store (kill switch, heartbeat, day-start equity)
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT 'jupiter',
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    mint TEXT NOT NULL,
    symbol TEXT,
    strategy TEXT,
    qty REAL NOT NULL,
    decimals INTEGER NOT NULL,
    usd_gross REAL NOT NULL,      -- USDC in (buy) / out (sell) at the quoted route, after sim slippage
    fee_usd REAL NOT NULL,        -- network base + priority fee
    price REAL NOT NULL,          -- effective USD per token
    quote_price REAL NOT NULL,    -- USD per token before sim slippage
    price_impact_pct REAL,
    slippage_bps INTEGER,
    route TEXT,
    stop_price REAL,
    tp_price REAL,
    meta TEXT
);
CREATE TRIGGER IF NOT EXISTS fills_no_update BEFORE UPDATE ON fills
BEGIN SELECT RAISE(ABORT, 'fills ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fills_no_delete BEFORE DELETE ON fills
BEGIN SELECT RAISE(ABORT, 'fills ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle INTEGER,
    kind TEXT NOT NULL,           -- entry | veto | exit | kill_switch | reconcile | error
    mint TEXT,
    symbol TEXT,
    strategy TEXT,
    signals TEXT,
    votes TEXT,
    reason TEXT,
    order_id TEXT
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decision log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decision log is append-only'); END;

CREATE TABLE IF NOT EXISTS positions (
    mint TEXT PRIMARY KEY,        -- instrument id: Solana mint, or hl:<coin> for Hyperliquid
    venue TEXT NOT NULL DEFAULT 'jupiter',
    symbol TEXT,
    strategy TEXT,
    qty REAL NOT NULL,
    decimals INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL,
    tp_price REAL,
    opened_at TEXT,
    entry_order_id TEXT,
    pool_address TEXT,
    mark_price REAL,
    mark_value REAL
);

CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL,
    cycle INTEGER,
    cash REAL,
    positions_value REAL,
    equity REAL,
    contributed REAL,             -- start capital + net paper deposits (so gains exclude deposits)
    sol_price REAL,               -- benchmarks for "vs buy-and-hold"
    btc_price REAL
);

CREATE TABLE IF NOT EXISTS funding (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL,
    mint TEXT NOT NULL,
    entry_order_id TEXT NOT NULL,
    rate REAL NOT NULL,           -- hourly funding rate
    hours REAL NOT NULL,
    notional_usd REAL NOT NULL,
    amount_usd REAL NOT NULL      -- > 0: paid by us (long with positive funding)
);
CREATE TRIGGER IF NOT EXISTS funding_no_update BEFORE UPDATE ON funding
BEGIN SELECT RAISE(ABORT, 'funding ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS funding_no_delete BEFORE DELETE ON funding
BEGIN SELECT RAISE(ABORT, 'funding ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS cash_movements (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('deposit','withdraw')),
    amount_usd REAL NOT NULL CHECK (amount_usd > 0),
    note TEXT
);
CREATE TRIGGER IF NOT EXISTS cash_no_update BEFORE UPDATE ON cash_movements
BEGIN SELECT RAISE(ABORT, 'cash movements are append-only'); END;
CREATE TRIGGER IF NOT EXISTS cash_no_delete BEFORE DELETE ON cash_movements
BEGIN SELECT RAISE(ABORT, 'cash movements are append-only'); END;

CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    epoch REAL NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    risk_level TEXT,
    brief TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str, start_usdc: float):
        self.path = path
        self.start_usdc = start_usdc
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        self.conn.executescript(SCHEMA)

    def _migrate(self):
        """Add columns introduced after a DB was created (ALTER is not blocked by the append-only triggers)."""
        adds = {"fills": [("venue", "TEXT NOT NULL DEFAULT 'jupiter'")],
                "positions": [("venue", "TEXT NOT NULL DEFAULT 'jupiter'")],
                "equity": [("contributed", "REAL"), ("sol_price", "REAL"), ("btc_price", "REAL")]}
        for table, cols in adds.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _x(self, sql: str, args=()):
        with self._lock:
            return self.conn.execute(sql, args)

    # ─── fills ───
    def append_fill(self, fill: dict) -> tuple[dict, bool]:
        """Insert a fill; if order_id already exists return the stored one (idempotent)."""
        existing = self.get_fill(fill["order_id"])
        if existing:
            return existing, False
        cols = ",".join(fill)
        self._x(f"INSERT INTO fills ({cols}) VALUES ({','.join('?' * len(fill))})", tuple(fill.values()))
        return self.get_fill(fill["order_id"]), True

    def get_fill(self, order_id: str) -> dict | None:
        rows = self._q("SELECT * FROM fills WHERE order_id=?", (order_id,))
        return rows[0] if rows else None

    def fills(self, limit: int = 200) -> list[dict]:
        return self._q("SELECT * FROM fills ORDER BY seq DESC LIMIT ?", (limit,))

    def cash(self) -> float:
        r = self._q("""SELECT
            COALESCE(SUM(CASE WHEN side='buy' THEN -usd_gross ELSE usd_gross END), 0) AS flow,
            COALESCE(SUM(fee_usd), 0) AS fees FROM fills""")[0]
        return self.start_usdc + self.net_deposits() + r["flow"] - r["fees"] - self.funding_paid()

    def add_funding(self, venue: str, mint: str, entry_order_id: str, rate: float, hours: float,
                    notional_usd: float) -> float:
        amount = notional_usd * rate * hours
        self._x("INSERT INTO funding (ts,venue,mint,entry_order_id,rate,hours,notional_usd,amount_usd) "
                "VALUES (?,?,?,?,?,?,?,?)", (now_iso(), venue, mint, entry_order_id, rate, hours, notional_usd, amount))
        return amount

    def funding_paid(self, entry_order_id: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(amount_usd),0) AS f FROM funding", ()
        if entry_order_id:
            sql, args = sql + " WHERE entry_order_id=?", (entry_order_id,)
        return self._q(sql, args)[0]["f"]

    def last_funding_ts(self, entry_order_id: str) -> str | None:
        rows = self._q("SELECT ts FROM funding WHERE entry_order_id=? ORDER BY seq DESC LIMIT 1", (entry_order_id,))
        return rows[0]["ts"] if rows else None

    # ─── paper deposits / withdrawals (simulated money only) ───
    def move_cash(self, kind: str, amount_usd: float, note: str = "") -> dict:
        if amount_usd <= 0:
            raise ValueError("amount must be positive")
        if kind == "withdraw" and amount_usd > self.cash() + 1e-9:
            raise ValueError(f"insufficient paper cash (${self.cash():.2f})")
        self._x("INSERT INTO cash_movements (ts,kind,amount_usd,note) VALUES (?,?,?,?)",
                (now_iso(), kind, amount_usd, note))
        return self.cash_movements(1)[0]

    def cash_movements(self, limit: int = 100) -> list[dict]:
        return self._q("SELECT * FROM cash_movements ORDER BY seq DESC LIMIT ?", (limit,))

    def net_deposits(self) -> float:
        return self._q("""SELECT COALESCE(SUM(CASE WHEN kind='deposit' THEN amount_usd ELSE -amount_usd END),0)
                          AS n FROM cash_movements""")[0]["n"]

    def positions_from_fills(self) -> dict[str, dict]:
        """Ledger truth: net qty and cost per mint, with stop/tp from the latest buy."""
        pos: dict[str, dict] = {}
        for f in self._q("SELECT * FROM fills ORDER BY seq"):
            p = pos.get(f["mint"])
            if f["side"] == "buy":
                if p is None:
                    p = pos[f["mint"]] = dict(mint=f["mint"], venue=f["venue"], symbol=f["symbol"], strategy=f["strategy"],
                                              qty=0.0, cost_usd=0.0, decimals=f["decimals"],
                                              opened_at=f["ts"], entry_order_id=f["order_id"])
                p["qty"] += f["qty"]
                p["cost_usd"] += f["usd_gross"] + f["fee_usd"]
                p["stop_price"], p["tp_price"] = f["stop_price"], f["tp_price"]
                p["pool_address"] = json.loads(f["meta"] or "{}").get("pool_address")
            elif p is not None:
                frac = min(f["qty"] / p["qty"], 1.0) if p["qty"] else 1.0
                p["cost_usd"] *= 1 - frac
                p["qty"] -= f["qty"]
                if p["qty"] <= 1e-12:
                    del pos[f["mint"]]
        for p in pos.values():
            p["entry_price"] = p["cost_usd"] / p["qty"]
        return pos

    def closed_trades(self) -> list[dict]:
        """Round trips (each sell matched against the open cost of that mint), newest first."""
        open_: dict[str, dict] = {}
        out = []
        for f in self._q("SELECT * FROM fills ORDER BY seq"):
            o = open_.get(f["mint"])
            if f["side"] == "buy":
                if o is None:
                    o = open_[f["mint"]] = {"qty": 0.0, "cost": 0.0, "entry_ts": f["ts"], "strategy": f["strategy"],
                                            "entry_id": f["order_id"], "fees": 0.0, "entry_price": f["price"],
                                            "entry_slip": f["slippage_bps"]}
                o["qty"] += f["qty"]
                o["cost"] += f["usd_gross"] + f["fee_usd"]
                o["fees"] += f["fee_usd"]
            elif o is not None and o["qty"] > 0:
                frac = min(f["qty"] / o["qty"], 1.0)
                cost = o["cost"] * frac
                funding = self.funding_paid(o["entry_id"]) * frac
                proceeds = f["usd_gross"] - f["fee_usd"] - funding
                out.append({"mint": f["mint"], "venue": f["venue"], "symbol": f["symbol"], "strategy": o["strategy"],
                            "entry_ts": o["entry_ts"], "exit_ts": f["ts"], "cost_usd": cost,
                            "entry_price": o["entry_price"], "exit_price": f["price"],
                            "fees_usd": o["fees"] * frac + f["fee_usd"], "funding_usd": funding,
                            "slippage_bps": [o["entry_slip"], f["slippage_bps"]],
                            "proceeds_usd": proceeds, "pnl_usd": proceeds - cost,
                            "pnl_pct": (proceeds - cost) / cost if cost else 0,
                            "reason": json.loads(f["meta"] or "{}").get("reason", ""),
                            "order_id": f["order_id"]})
                o["qty"] -= f["qty"]
                o["cost"] -= cost
                if o["qty"] <= 1e-12:
                    del open_[f["mint"]]
        return list(reversed(out))

    def realized_pnl(self) -> float:
        """Cash + cost basis of what is still open − contributed capital = realized P&L."""
        return (self.cash() + sum(p["cost_usd"] for p in self.positions_from_fills().values())
                - self.start_usdc - self.net_deposits())

    # ─── positions (materialized) ───
    def positions(self) -> list[dict]:
        return self._q("SELECT * FROM positions ORDER BY opened_at")

    def upsert_position(self, p: dict):
        cols = [c for c in ("mint", "venue", "symbol", "strategy", "qty", "decimals", "cost_usd", "entry_price",
                            "stop_price", "tp_price", "opened_at", "entry_order_id", "pool_address",
                            "mark_price", "mark_value") if c in p]
        self._x(f"INSERT OR REPLACE INTO positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(p[c] for c in cols))

    def set_mark(self, mint: str, price: float, value: float):
        self._x("UPDATE positions SET mark_price=?, mark_value=? WHERE mint=?", (price, value, mint))

    def delete_position(self, mint: str):
        self._x("DELETE FROM positions WHERE mint=?", (mint,))

    # ─── decisions ───
    def log_decision(self, kind: str, *, cycle=None, mint=None, symbol=None, strategy=None,
                     signals=None, votes=None, reason="", order_id=None) -> int:
        cur = self._x(
            "INSERT INTO decisions (ts,cycle,kind,mint,symbol,strategy,signals,votes,reason,order_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), cycle, kind, mint, symbol, strategy, json.dumps(signals or {}, default=str),
             json.dumps(votes or {}, default=str), reason, order_id))
        from src.solana import logbuf, notify
        source = logbuf.label(self.path)
        logbuf.add(source, logbuf.DECISION_LEVEL.get(kind, "INFO"),
                   f"{kind}{f' {symbol}' if symbol else ''}{f' [{strategy}]' if strategy else ''}: {reason}")
        if kind in ("entry", "exit") and symbol:
            trade = next((t for t in self.closed_trades() if t["symbol"] == symbol), None) if kind == "exit" else None
            notify.trade_event(source, kind, symbol, strategy, reason, trade)
        return cur.lastrowid

    def decisions(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM decisions", []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        rows = self._q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["signals"], r["votes"] = json.loads(r["signals"]), json.loads(r["votes"])
        return rows

    # ─── research briefs ───
    def add_research(self, mint: str, symbol: str, risk_level: str, brief: dict):
        self._x("INSERT INTO research (ts,epoch,mint,symbol,risk_level,brief) VALUES (?,?,?,?,?,?)",
                (now_iso(), time.time(), mint, symbol, risk_level, json.dumps(brief, default=str)))

    def latest_research(self, mint: str) -> dict | None:
        rows = self._q("SELECT * FROM research WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if not rows:
            return None
        rows[0]["brief"] = json.loads(rows[0]["brief"])
        return rows[0]

    def research(self, limit: int = 30) -> list[dict]:
        rows = self._q("""SELECT * FROM research WHERE id IN (SELECT MAX(id) FROM research GROUP BY mint)
                          ORDER BY id DESC LIMIT ?""", (limit,))
        for r in rows:
            r["brief"] = json.loads(r["brief"])
        return rows

    # ─── equity / state ───
    def record_equity(self, cycle: int, cash: float, positions_value: float, bench: dict | None = None):
        bench = bench or {}
        self._x("INSERT INTO equity (ts,cycle,cash,positions_value,equity,contributed,sol_price,btc_price) "
                "VALUES (?,?,?,?,?,?,?,?)", (now_iso(), cycle, cash, positions_value, cash + positions_value,
                                             self.contributed(), bench.get("SOL"), bench.get("BTC")))

    def contributed(self) -> float:
        return self.start_usdc + self.net_deposits()

    def equity_history(self, limit: int = 500) -> list[dict]:
        return list(reversed(self._q("SELECT * FROM equity ORDER BY rowid DESC LIMIT ?", (limit,))))

    def get_state(self, key: str, default=None):
        rows = self._q("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value):
        self._x("INSERT OR REPLACE INTO state (key,value) VALUES (?,?)", (key, json.dumps(value)))
