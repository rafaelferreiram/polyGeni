"""
Bot scanner: scheduled loop that finds and optionally acts on opportunities.
"""
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session
from src.analysis.engine import run_full_scan
from src.analysis.short_term import scan_short_term_markets
from src.bot.trader import execute_opportunity, sync_positions
from src.polymarket.client import get_balance
from src.database import SessionLocal
from src.config import BOT_SCAN_INTERVAL_SEC
from src.models import Position

logger = logging.getLogger("scanner")

TARGET_USDC = 30.0  # weekend goal

_state = {
    "running": False,
    "auto_trade": False,
    "last_scan": None,
    "last_opportunities": [],
    "scheduler": None,
    "thinking_log": [],  # list of {timestamp, cycle, message, type}
}

_cycle = 0


def _log_thought(msg: str, kind: str = "info"):
    """Append a thought entry to the in-memory thinking log (keep last 200)."""
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "cycle": _cycle,
        "message": msg,
        "type": kind,  # info | decision | trade | warning
    }
    _state["thinking_log"].append(entry)
    if len(_state["thinking_log"]) > 200:
        _state["thinking_log"] = _state["thinking_log"][-200:]


def get_state() -> dict:
    return {
        "running": _state["running"],
        "auto_trade": _state["auto_trade"],
        "last_scan": _state["last_scan"].isoformat() if _state["last_scan"] else None,
        "opportunity_count": len(_state["last_opportunities"]),
    }


def _scan_job():
    global _cycle
    if not _state["running"]:
        return

    _cycle += 1
    db: Session = SessionLocal()
    try:
        logger.info("[scanner] Running scan...")
        bankroll = get_balance()
        to_goal = TARGET_USDC - bankroll
        _log_thought(
            f"Cycle #{_cycle} — Balance: ${bankroll:.2f} | Goal: ${TARGET_USDC:.2f} | Need: ${to_goal:.2f}",
            "info"
        )

        # Always scan short-term markets first (resolving within 7 days)
        short_term = scan_short_term_markets(bankroll, days=7)
        long_term   = run_full_scan(bankroll)
        _log_thought(
            f"Scan complete — {len(short_term)} short-term, {len(long_term)} long-term candidates",
            "info"
        )

        # Merge: short-term first, then long-term (deduped)
        seen = set()
        opportunities = []
        for o in short_term + long_term:
            mid = o.get("market_id")
            if mid not in seen:
                seen.add(mid)
                opportunities.append(o)

        # Log top opportunities
        for o in opportunities[:5]:
            _log_thought(
                f"  [{o['category']}] {o['recommended_side']} {o['edge']*100:.1f}% edge | "
                f"Kelly ${o['kelly_size_usdc']:.2f} | {o['question'][:60]}",
                "decision"
            )

        _state["last_opportunities"] = opportunities
        _state["last_scan"] = datetime.utcnow()

        open_count = db.query(Position).filter_by(is_open=True).count()
        sync_positions(db)

        if _state["auto_trade"] and opportunities:
            # Try candidates in order: short-term first, then by edge descending
            # Keep trying until one succeeds or all are exhausted
            traded = False
            for candidate in opportunities:
                if candidate["edge"] < 0.04:
                    _log_thought(
                        f"Best remaining edge {candidate['edge']:.1%} < 4% threshold — no trade this cycle",
                        "warning"
                    )
                    logger.info(f"[scanner] Best edge {candidate['edge']:.1%} — below threshold, skipping all")
                    break
                result = execute_opportunity(candidate, bankroll, open_count, db, dry_run=False)
                logger.info(f"[scanner] Trade attempt: {result}")
                if result.get("success"):
                    _log_thought(
                        f"TRADE PLACED: {candidate['recommended_side']} on '{candidate['question'][:60]}' "
                        f"| ${candidate['kelly_size_usdc']:.2f} @ {candidate['market_prob']*100:.1f}¢ "
                        f"| Edge {candidate['edge']*100:.1f}%",
                        "trade"
                    )
                    traded = True
                    break
                reason = result.get("reason", "")
                _log_thought(f"Skipped '{candidate['question'][:50]}' — {reason}", "warning")
                # Stop only on hard blockers (positions full, low bankroll)
                if "Max open positions" in reason or "Bankroll" in reason:
                    break
            if not traded:
                logger.info("[scanner] No trade placed this cycle")
        else:
            msg = f"Found {len(opportunities)} opportunities (auto-trade OFF)"
            _log_thought(msg, "info")
            logger.info(f"[scanner] {msg}")

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


def get_thinking_log() -> list[dict]:
    return list(reversed(_state["thinking_log"]))  # newest first


def get_goal_info() -> dict:
    return {
        "target_usdc": TARGET_USDC,
        "start_usdc": 13.25,  # balance when we set the goal
        "cycle": _cycle,
    }
