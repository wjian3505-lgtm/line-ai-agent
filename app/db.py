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


def _make_engine():
    url = settings.database_url
    connect_args = {}
    if url.startswith("sqlite"):
        # 確保 SQLite 檔案所在資料夾存在
        os.makedirs("data", exist_ok=True)
        connect_args = {"check_same_thread": False}
    return create_engine(url, echo=False, pool_pre_ping=True, connect_args=connect_args)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """建立所有資料表（若不存在）。"""
    from . import models  # noqa: F401 匯入以註冊 ORM 模型

    Base.metadata.create_all(bind=engine)


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
