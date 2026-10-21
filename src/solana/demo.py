"""
End-to-end demo on a DUMMY paper account (separate DB, default ./demo.db):
deposit -> forced demo entry through the real Safety/Debate/Risk/Executor chain with REAL Jupiter
quotes -> mark-to-market -> stop/TP exit -> withdraw. Proves the plumbing, not the strategy.

    python -m src.solana.demo            # then: PAPER_DB_PATH=demo.db uvicorn src.main:app
"""
import argparse
import os
import time
from dataclasses import dataclass

from src.solana import config as cfg
from src.solana.agents import DebateAgent, ExecutorAgent, RiskAgent, SafetyAgent, ScoutAgent
from src.solana.broker import PaperBroker
from src.solana.engine import PaperEngine
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
    eng = PaperEngine(ledger, ScoutAgent(max_n=3), SafetyAgent(), [DemoAlwaysEnter()], DebateAgent(),
                      RiskAgent(max_positions=2), ExecutorAgent(PaperBroker(ledger)), research=ResearchAgent(ledger))
    print("deposit:", ledger.move_cash("deposit", 50, "demo deposit"))
    for i in range(args.cycles):
        print(f"cycle {i + 1}:", eng.run_cycle())
        time.sleep(5)
    print("withdraw:", ledger.move_cash("withdraw", 20, "demo withdraw"))
    print("fills:", len(ledger.fills()), "closed trades:", len(ledger.closed_trades()),
          "cash:", round(ledger.cash(), 4), "contributed:", ledger.contributed())
    for t in ledger.closed_trades():
        print(f"  {t['symbol']}: cost ${t['cost_usd']:.4f} -> ${t['proceeds_usd']:.4f} pnl ${t['pnl_usd']:+.4f} ({t['reason']})")


if __name__ == "__main__":
    main()
