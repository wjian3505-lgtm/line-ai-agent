"""ORM 資料模型：行程、記帳、筆記。皆以 LINE user_id 做資料隔離。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Schedule(Base):
    """行程 / 待辦。"""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    done: Mapped[int] = mapped_column(Integer, default=0)  # 0=未完成 1=完成
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Expense(Base):
    """記帳。"""

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(64), default="其他")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    spent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    kind: Mapped[str] = mapped_column(String(16), default="expense")  # expense / refund / income
    related_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Note(Base):
    """一般備忘 / 筆記。"""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockQueryLog(Base):
    """使用者查詢個股的紀錄（用於「今天查了哪些股票」）。"""

    __tablename__ = "stock_query_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    queried_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class Holding(Base):
    """持股 / 買入紀錄。"""

    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    buy_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float, default=1000)  # 股數；1 張 = 1000 股
    bought_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    closed: Mapped[int] = mapped_column(Integer, default=0)  # 0=持有中 1=已結清
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockSale(Base):
    """賣出紀錄。買入仍在 holdings，這裡只記成交與已實現損益。"""

    __tablename__ = "stock_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sell_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockSaleLot(Base):
    """一筆賣出扣到哪些買入（先進先出）。"""

    __tablename__ = "stock_sale_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sale_id: Mapped[int] = mapped_column(Integer, index=True)
    holding_id: Mapped[int] = mapped_column(Integer, index=True)
    quantity: Mapped[float] = mapped_column(Float)
    buy_price: Mapped[float] = mapped_column(Float)
