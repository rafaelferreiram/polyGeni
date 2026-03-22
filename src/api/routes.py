from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from src.database import get_db
from src.models import Trade, Position, Opportunity, PortfolioSnapshot
from src.bot import scanner
from src.bot.trader import execute_opportunity, sync_positions
from src.polymarket.client import get_balance
from src.config import BOT_KELLY_FRACTION, BOT_MAX_POSITION_PCT
from src.bot.risk import MAX_OPEN_POSITIONS
from pydantic import BaseModel

router = APIRouter()


# ─── Status & Control ───────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    try:
        balance = get_balance()
    except Exception:
        balance = None
    return {**scanner.get_state(), "balance_usdc": balance}


@router.post("/bot/start")
def start_bot(auto_trade: bool = False):
    return scanner.start_bot(auto_trade=auto_trade)


@router.post("/bot/stop")
def stop_bot():
    return scanner.stop_bot()


@router.post("/bot/scan")
def trigger_scan(db: Session = Depends(get_db)):
    opportunities = scanner.run_scan_now(db)
    return {"count": len(opportunities), "opportunities": opportunities[:10]}


# ─── Opportunities ───────────────────────────────────────────────────────────

@router.get("/opportunities")
def get_opportunities(db: Session = Depends(get_db), limit: int = 20):
    records = (
        db.query(Opportunity)
        .order_by(Opportunity.scanned_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "market_id": r.market_id,
            "question": r.question,
            "category": r.category,
            "recommended_side": r.recommended_side,
            "market_prob": r.market_prob,
            "estimated_prob": r.estimated_prob,
            "edge": r.edge,
            "kelly_size_usdc": r.kelly_size_usdc,
            "reasoning": r.reasoning,
            "acted_on": r.acted_on,
            "scanned_at": r.scanned_at.isoformat(),
        }
        for r in records
    ]


@router.get("/opportunities/live")
def get_live_opportunities():
    """Returns the latest in-memory scan results (not persisted)."""
    return scanner.get_latest_opportunities()


@router.get("/thinking")
def get_thinking(db: Session = Depends(get_db)):
    """Returns bot decision log and goal progress."""
    goal = scanner.get_goal_info()
    try:
        balance = get_balance()
    except Exception:
        balance = 0.0

    # Use full portfolio value (USDC + current position value) for progress
    positions = db.query(Position).filter_by(is_open=True).all()
    invested = sum(p.current_value or p.cost_basis for p in positions)
    portfolio_value = balance + invested

    span = goal["target_usdc"] - goal["start_usdc"]
    gained = portfolio_value - goal["start_usdc"]
    progress = round(max(0, min(1, gained / span)), 4) if span > 0 else 0

    return {
        "goal": goal,
        "balance_usdc": balance,
        "portfolio_value": round(portfolio_value, 2),
        "progress": progress,
        "log": scanner.get_thinking_log(),
    }


# ─── Trades ──────────────────────────────────────────────────────────────────

@router.get("/trades")
def get_trades(db: Session = Depends(get_db), limit: int = 50):
    trades = db.query(Trade).order_by(Trade.created_at.desc()).limit(limit).all()
    return [
        {
            "id": t.id,
            "order_id": t.order_id,
            "question": t.question,
            "category": t.category,
            "side": t.side,
            "price": t.price,
            "usdc_spent": t.usdc_spent,
            "edge": t.edge,
            "status": t.status,
            "pnl": t.pnl,
            "created_at": t.created_at.isoformat(),
        }
        for t in trades
    ]


# ─── Positions ───────────────────────────────────────────────────────────────

@router.get("/positions")
def get_positions(db: Session = Depends(get_db)):
    sync_positions(db)
    positions = db.query(Position).filter_by(is_open=True).all()
    return [
        {
            "id": p.id,
            "question": p.question,
            "category": p.category,
            "side": p.side,
            "shares": p.shares,
            "avg_price": p.avg_price,
            "current_price": p.current_price,
            "cost_basis": p.cost_basis,
            "current_value": p.current_value,
            "unrealized_pnl": p.unrealized_pnl,
            "opened_at": p.opened_at.isoformat(),
        }
        for p in positions
    ]


# ─── Portfolio Summary ────────────────────────────────────────────────────────

@router.get("/portfolio")
def get_portfolio(db: Session = Depends(get_db)):
    try:
        balance = get_balance()
    except Exception:
        balance = 0.0

    trades = db.query(Trade).all()
    total_spent = sum(t.usdc_spent for t in trades if t.status != "cancelled")
    total_pnl = sum(t.pnl for t in trades if t.status in ("won", "lost"))
    win_count = sum(1 for t in trades if t.status == "won")
    loss_count = sum(1 for t in trades if t.status == "lost")
    total_resolved = win_count + loss_count

    positions = db.query(Position).filter_by(is_open=True).all()
    unrealized = sum(p.unrealized_pnl for p in positions)
    invested = sum(p.cost_basis for p in positions)

    portfolio_value = balance + invested + unrealized

    return {
        "balance_usdc": balance,
        "portfolio_value": round(portfolio_value, 2),
        "currently_invested": round(invested, 2),
        "total_invested": round(total_spent, 2),
        "realized_pnl": round(total_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(total_pnl + unrealized, 2),
        "win_rate": round(win_count / total_resolved, 3) if total_resolved > 0 else None,
        "total_trades": len(trades),
        "open_positions": len(positions),
    }


# ─── Manual Trade ────────────────────────────────────────────────────────────

class ManualTradeRequest(BaseModel):
    market_id: str
    question: str
    category: str
    recommended_side: str
    market_prob: float
    estimated_prob: float
    edge: float
    kelly_size_usdc: float
    yes_token_id: str
    reasoning: str = "Manual trade"


@router.get("/portfolio/history")
def get_portfolio_history(db: Session = Depends(get_db), period: str = "hourly"):
    """Returns portfolio value snapshots grouped by hour or day."""
    from sqlalchemy import func
    snapshots = (
        db.query(PortfolioSnapshot)
        .order_by(PortfolioSnapshot.timestamp.asc())
        .all()
    )
    if not snapshots:
        return []

    # Group by hour or day
    grouped = {}
    for s in snapshots:
        if period == "daily":
            key = s.timestamp.strftime("%Y-%m-%d")
        else:
            key = s.timestamp.strftime("%Y-%m-%dT%H:00")
        grouped[key] = {
            "timestamp": key,
            "portfolio_value": round(s.portfolio_value, 2),
            "balance_usdc": round(s.balance_usdc, 2),
            "open_positions": s.open_positions,
            "trade_count": s.trade_count,
        }

    return list(grouped.values())


@router.get("/aggression")
def get_aggression(db: Session = Depends(get_db)):
    """Returns bot aggressiveness index (0=very conservative, 100=very aggressive)."""
    try:
        balance = get_balance()
    except Exception:
        balance = 1.0

    open_count = db.query(Position).filter_by(is_open=True).count()
    recent_trades = (
        db.query(Trade)
        .filter(Trade.status != "cancelled")
        .order_by(Trade.created_at.desc())
        .limit(10)
        .all()
    )

    # Kelly fraction component (0.25 → 25pts, 0.5 → 50pts, 1.0 → 100pts)
    kelly_score = min(BOT_KELLY_FRACTION / 1.0, 1.0) * 40

    # Positions fill rate (how full is our book)
    fill_score = min(open_count / MAX_OPEN_POSITIONS, 1.0) * 25

    # Average bet size as % of max allowed
    if recent_trades and balance > 0:
        avg_bet_pct = sum(t.usdc_spent for t in recent_trades) / len(recent_trades) / max(balance, 1)
        bet_score = min(avg_bet_pct / BOT_MAX_POSITION_PCT, 1.0) * 20
    else:
        bet_score = 10

    # Average edge taken (lower edge = more aggressive, higher = more selective/conservative)
    if recent_trades:
        avg_edge = sum(t.edge for t in recent_trades) / len(recent_trades)
        # Invert: edge 0.05 = aggressive (high score), edge 0.50 = conservative (low score)
        edge_score = max(0, (0.30 - avg_edge) / 0.30) * 15
    else:
        edge_score = 7

    score = round(kelly_score + fill_score + bet_score + edge_score)
    score = max(0, min(100, score))

    if score < 30:
        label = "Very Conservative"
    elif score < 50:
        label = "Conservative"
    elif score < 65:
        label = "Moderate"
    elif score < 80:
        label = "Aggressive"
    else:
        label = "Very Aggressive"

    return {
        "score": score,
        "label": label,
        "components": {
            "kelly_fraction": BOT_KELLY_FRACTION,
            "positions_fill": round(open_count / MAX_OPEN_POSITIONS, 2),
            "avg_edge": round(sum(t.edge for t in recent_trades) / len(recent_trades), 3) if recent_trades else 0,
            "avg_bet_pct": round(bet_score / 20, 2),
        }
    }


@router.post("/trade/manual")
def manual_trade(req: ManualTradeRequest, dry_run: bool = True, db: Session = Depends(get_db)):
    try:
        balance = get_balance()
    except Exception:
        raise HTTPException(status_code=503, detail="Cannot reach Polymarket")

    open_count = db.query(Position).filter_by(is_open=True).count()
    result = execute_opportunity(req.model_dump(), balance, open_count, db, dry_run=dry_run)
    return result
