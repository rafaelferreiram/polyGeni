from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.database import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    market_id: Mapped[str] = mapped_column(String)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String)  # bitcoin | sports | events
    side: Mapped[str] = mapped_column(String)       # YES | NO
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    usdc_spent: Mapped[float] = mapped_column(Float)
    estimated_prob: Mapped[float] = mapped_column(Float)
    market_prob: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default="open")  # open | won | lost | cancelled
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    token_id: Mapped[str] = mapped_column(String)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    shares: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    cost_basis: Mapped[float] = mapped_column(Float)
    current_value: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[str] = mapped_column(String, index=True)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String)
    recommended_side: Mapped[str] = mapped_column(String)
    market_prob: Mapped[float] = mapped_column(Float)
    estimated_prob: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    kelly_size_usdc: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str] = mapped_column(Text)
    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
