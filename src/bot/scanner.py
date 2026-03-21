"""
Bot scanner: scheduled loop that finds and optionally acts on opportunities.
"""
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session
from src.analysis.engine import run_full_scan
from src.bot.trader import execute_opportunity, sync_positions
from src.polymarket.client import get_balance
from src.database import SessionLocal
from src.config import BOT_SCAN_INTERVAL_SEC
from src.models import Position

logger = logging.getLogger("scanner")

_state = {
    "running": False,
    "auto_trade": False,
    "last_scan": None,
    "last_opportunities": [],
    "scheduler": None,
}


def get_state() -> dict:
    return {
        "running": _state["running"],
        "auto_trade": _state["auto_trade"],
        "last_scan": _state["last_scan"].isoformat() if _state["last_scan"] else None,
        "opportunity_count": len(_state["last_opportunities"]),
    }


def _scan_job():
    if not _state["running"]:
        return

    db: Session = SessionLocal()
    try:
        logger.info("[scanner] Running scan...")
        bankroll = get_balance()
        opportunities = run_full_scan(bankroll)
        _state["last_opportunities"] = opportunities
        _state["last_scan"] = datetime.utcnow()

        open_count = db.query(Position).filter_by(is_open=True).count()
        sync_positions(db)

        if _state["auto_trade"] and opportunities:
            best = opportunities[0]
            if best["edge"] >= 0.05:
                result = execute_opportunity(best, bankroll, open_count, db, dry_run=False)
                logger.info(f"[scanner] Trade result: {result}")
            else:
                logger.info(f"[scanner] Best edge {best['edge']:.1%} — below threshold, skipping")
        else:
            logger.info(f"[scanner] Found {len(opportunities)} opportunities (auto-trade OFF)")

    except Exception as e:
        logger.error(f"[scanner] Error: {e}")
    finally:
        db.close()


def start_bot(auto_trade: bool = False):
    _state["running"] = True
    _state["auto_trade"] = auto_trade

    if _state["scheduler"] is None:
        scheduler = BackgroundScheduler()
        scheduler.add_job(_scan_job, "interval", seconds=BOT_SCAN_INTERVAL_SEC, id="scan")
        scheduler.start()
        _state["scheduler"] = scheduler

    # Run immediately on start
    _scan_job()
    return {"status": "started", "auto_trade": auto_trade}


def stop_bot():
    _state["running"] = False
    if _state["scheduler"]:
        _state["scheduler"].pause()
    return {"status": "stopped"}


def run_scan_now(db: Session) -> list[dict]:
    bankroll = get_balance()
    opportunities = run_full_scan(bankroll)
    _state["last_opportunities"] = opportunities
    _state["last_scan"] = datetime.utcnow()
    return opportunities


def get_latest_opportunities() -> list[dict]:
    return _state["last_opportunities"]
