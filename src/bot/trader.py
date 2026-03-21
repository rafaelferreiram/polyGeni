"""
Trade execution: places orders and records everything to the DB.
"""
from datetime import datetime
from sqlalchemy.orm import Session
from src.polymarket import client as poly
from src.models import Trade, Position, Opportunity
from src.bot.risk import check_trade


def execute_opportunity(
    opportunity: dict,
    bankroll: float,
    open_positions: int,
    db: Session,
    dry_run: bool = False,
) -> dict:
    """
    Validates, places, and records a trade.
    dry_run=True: validates and records opportunity only, no real order.
    """
    approved, reason = check_trade(opportunity, bankroll, open_positions)
    if not approved:
        return {"success": False, "reason": reason}

    side = opportunity["recommended_side"]  # YES or NO
    token_id = opportunity["yes_token_id"]  # always YES for position tracking / sync
    market_prob = opportunity["market_prob"]  # always YES price
    size_usdc = opportunity["kelly_size_usdc"]

    if side == "NO":
        # Buy the NO token at the NO price
        no_token_id = opportunity.get("no_token_id") or token_id
        no_price = round(1.0 - market_prob, 4)
        order_token_id = no_token_id
        price = no_price
    else:
        order_token_id = token_id
        price = market_prob

    if dry_run:
        opp_record = Opportunity(
            market_id=opportunity["market_id"],
            question=opportunity["question"],
            category=opportunity["category"],
            recommended_side=side,
            market_prob=opportunity["market_prob"],
            estimated_prob=opportunity["estimated_prob"],
            edge=opportunity["edge"],
            kelly_size_usdc=size_usdc,
            reasoning=opportunity["reasoning"],
            acted_on=False,
        )
        db.add(opp_record)
        db.commit()
        return {"success": True, "dry_run": True, "opportunity": opportunity}

    try:
        response = poly.place_order(
            token_id=order_token_id,
            price=price,
            size_usdc=size_usdc,
            side="BUY",
        )
    except Exception as e:
        return {"success": False, "reason": str(e)}

    order_id = response.get("orderID") or response.get("order_id", "unknown")
    status = response.get("status", "unknown")

    trade = Trade(
        order_id=order_id,
        market_id=opportunity["market_id"],
        question=opportunity["question"],
        category=opportunity["category"],
        side=side,
        price=price,
        size=round(size_usdc / price, 2),
        usdc_spent=size_usdc,
        estimated_prob=opportunity["estimated_prob"],
        market_prob=opportunity["market_prob"],
        edge=opportunity["edge"],
        status="open" if status in ("live", "matched", "delayed") else "cancelled",
    )
    db.add(trade)

    if trade.status == "open":
        existing = db.query(Position).filter_by(market_id=opportunity["market_id"]).first()
        if existing:
            total_cost = existing.cost_basis + size_usdc
            total_shares = existing.shares + trade.size
            existing.avg_price = total_cost / total_shares if total_shares > 0 else price
            existing.shares = total_shares
            existing.cost_basis = total_cost
        else:
            pos = Position(
                market_id=opportunity["market_id"],
                token_id=token_id,
                question=opportunity["question"],
                category=opportunity["category"],
                side=side,
                shares=trade.size,
                avg_price=price,
                cost_basis=size_usdc,
            )
            db.add(pos)

    opp_record = Opportunity(
        market_id=opportunity["market_id"],
        question=opportunity["question"],
        category=opportunity["category"],
        recommended_side=side,
        market_prob=opportunity["market_prob"],
        estimated_prob=opportunity["estimated_prob"],
        edge=opportunity["edge"],
        kelly_size_usdc=size_usdc,
        reasoning=opportunity["reasoning"],
        acted_on=True,
    )
    db.add(opp_record)
    db.commit()

    return {"success": True, "order_id": order_id, "status": status, "trade": trade}


def sync_positions(db: Session):
    """Update current prices and unrealized PnL for all open positions.
    Auto-closes positions that are essentially resolved (value < 3% of cost).
    """
    open_positions = db.query(Position).filter_by(is_open=True).all()
    for pos in open_positions:
        yes_mid = poly.get_midpoint(pos.token_id)
        if yes_mid is not None:
            # For YES positions: value tracks YES midpoint
            # For NO positions: value tracks (1 - YES midpoint) = NO midpoint
            price = yes_mid if pos.side == "YES" else (1.0 - yes_mid)
            pos.current_price = round(price, 4)
            pos.current_value = round(pos.shares * price, 2)
            pos.unrealized_pnl = round(pos.current_value - pos.cost_basis, 2)
            # Auto-close positions where value has dropped to <3% of cost basis
            # (market has effectively resolved against us)
            if pos.cost_basis > 0 and pos.current_value < pos.cost_basis * 0.03:
                pos.is_open = False
                pos.pnl = pos.unrealized_pnl
    db.commit()
