"""資料庫連線與 Session 管理。

本地未設定 DATABASE_URL 時使用 SQLite；部署到 Zeabur 時設定
PostgreSQL 連線字串即可無縫切換（因為雲端檔案系統是暫存的）。
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _normalize_database_url(url: str) -> str:
    """雲端常給 postgresql://，新版 SQLAlchemy 會去載 psycopg v3。改走已安裝的 psycopg2。"""
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://") :]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql+psycopg2://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


def _make_engine():
    url = _normalize_database_url(settings.database_url)
    connect_args = {}
    if url.startswith("sqlite"):
        # 確保 SQLite 檔案所在資料夾存在
        os.makedirs("data", exist_ok=True)
        connect_args = {"check_same_thread": False}
    return create_engine(url, echo=False, pool_pre_ping=True, connect_args=connect_args)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """建立所有資料表（若不存在），並補上既有 expenses 缺的欄位。"""
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401 匯入以註冊 ORM 模型

    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    if "expenses" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("expenses")}
    with engine.begin() as conn:
        if "kind" not in cols:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN kind VARCHAR(16) DEFAULT 'expense'"))
        if "related_id" not in cols:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN related_id INTEGER"))


@contextmanager
def session_scope():
    """提供交易範圍的 Session，離開時自動 commit / rollback。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
