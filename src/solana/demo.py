"""
End-to-end demo on a DUMMY paper account (separate DB, default ./demo.db), both venues:
deposit -> forced demo entries through the real Safety/Research/Debate/Risk/Executor chain with REAL
Jupiter quotes and REAL Hyperliquid order books -> funding -> mark-to-market -> exits -> withdraw,
then an independent recomputation of cash from the raw ledger rows. Proves the plumbing, not the strategy.

    make demo        # runs this, then serves the dashboard for demo.db on http://127.0.0.1:8001/paper
"""
import argparse
import os
import time
from dataclasses import dataclass

from src.solana import config as cfg
from src.solana import market_data as md
from src.solana import hyperliquid as hl
from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, SafetyAgent, ScoutAgent
from src.solana.broker import PaperBroker
from src.solana.engine import PaperEngine, bench_prices
from src.solana.ledger import Ledger
from src.solana.research import ResearchAgent
from src.solana.strategies import Signal


@dataclass
class DemoAlwaysEnter:
    """DEMO ONLY: always signals, with a tiny stop/TP so the next cycle exits."""
    stop_pct: float = 0.002
    tp_pct: float = 0.002
    capital_pct: float = 0.5
    name: str = "demo"

    def signal(self, candles):
        if not candles:
            return None
        return Signal(self.name, candles[-1]["close"], self.stop_pct, self.tp_pct, "demo: forced entry to test plumbing")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="demo.db")
    ap.add_argument("--cycles", type=int, default=3)
    args = ap.parse_args()
    if os.path.abspath(args.db) == os.path.abspath(cfg.PAPER_DB_PATH):
        raise SystemExit("refusing to run the demo on the main paper DB")

    ledger = Ledger(args.db, cfg.PAPER_START_USDC)
    eng = PaperEngine(ledger, [ScoutAgent(max_n=3), hl.HyperliquidScout(["BTC", "xyz:GOLD"])], SafetyAgent(),
                      [DemoAlwaysEnter()], DebateAgent(), RiskAgent(max_positions=4, max_exposure_pct=0.8, max_entries_per_day=100),
                      ExecutorAgent(PaperBroker(ledger), hl.HyperliquidPaperBroker(ledger)),
                      candles_fn={"jupiter": md.pool_candles, "hyperliquid": hl.candles},
                      research=ResearchAgent(ledger), bench_fn=bench_prices,
                      max_hold_h=0)   # demo: every position exits on the next cycle
    print("deposit:", ledger.move_cash("deposit", 50, "demo deposit"))
    for i in range(args.cycles):
        print(f"cycle {i + 1}:", eng.run_cycle())
        time.sleep(5)
    print("withdraw:", ledger.move_cash("withdraw", 20, "demo withdraw"))
    trades = ledger.closed_trades()
    print("fills:", len(ledger.fills()), "closed trades:", len(trades), "venues:", sorted({t["venue"] for t in trades}),
          "cash:", round(ledger.cash(), 4), "contributed:", ledger.contributed())
    for t in trades:
        print(f"  [{t['venue']}] {t['symbol']}: cost ${t['cost_usd']:.4f} -> ${t['proceeds_usd']:.4f} "
              f"pnl ${t['pnl_usd']:+.4f} fees ${t['fees_usd']:.4f} funding ${t['funding_usd']:.6f} ({t['reason']})")

    # Independent check: rebuild cash from raw rows (not via Ledger.cash()).
    c = ledger.conn
    flows = c.execute("SELECT COALESCE(SUM(CASE side WHEN 'buy' THEN -usd_gross-fee_usd ELSE usd_gross-fee_usd END),0) "
                      "FROM fills").fetchone()[0]
    moves = c.execute("SELECT COALESCE(SUM(CASE kind WHEN 'deposit' THEN amount_usd ELSE -amount_usd END),0) "
                      "FROM cash_movements").fetchone()[0]
    fund = c.execute("SELECT COALESCE(SUM(amount_usd),0) FROM funding").fetchone()[0]
    rebuilt = cfg.PAPER_START_USDC + moves + flows - fund
    assert abs(rebuilt - ledger.cash()) < 1e-6, (rebuilt, ledger.cash())
    open_cost = sum(p["cost_usd"] for p in ledger.positions_from_fills().values())
    realized = sum(t["pnl_usd"] for t in trades)
    assert abs((ledger.cash() + open_cost - ledger.contributed()) - realized) < 1e-6
    print(f"CHECK OK: cash rebuilt from rows ${rebuilt:.4f} == ledger ${ledger.cash():.4f}; "
          f"realized P&L ${realized:+.4f} matches cash + open cost − contributed")


if __name__ == "__main__":
    main()
