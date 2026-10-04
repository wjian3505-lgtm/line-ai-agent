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


# 畫面上的 #編號依帳號分開。資料庫 id 維持全表流水號。
_USER_SEQ_TABLES = ("expenses", "schedules", "notes", "holdings", "stock_sales")


def _backfill_user_seq(conn, table: str) -> None:
    """補上既有資料的個人編號。

    最早那筆紀錄的帳號沿用原本的 id，所以舊的 #135 還是 #135，空號也留著。
    其他帳號依自己的紀錄從 #1 重新編號。
    """
    from sqlalchemy import text

    rows = conn.execute(text(f"SELECT id, user_id, seq FROM {table} ORDER BY id")).fetchall()
    if not rows or all(row[2] is not None for row in rows):
        return
    anchor = rows[0][1]
    rank: dict[str, int] = {}
    for rid, uid, existing in rows:
        if uid == anchor:
            seq = int(rid)
        else:
            rank[uid] = rank.get(uid, 0) + 1
            seq = rank[uid]
        if existing is None:
            conn.execute(
                text(f"UPDATE {table} SET seq = :seq WHERE id = :id"),
                {"seq": seq, "id": rid},
            )
    conn.execute(
        text(
            f"INSERT INTO user_seq_counters (user_id, bucket, last_seq) "
            f"SELECT e.user_id, :bucket, MAX(e.seq) FROM {table} e "
            f"WHERE e.seq IS NOT NULL AND NOT EXISTS ("
            f"SELECT 1 FROM user_seq_counters c "
            f"WHERE c.user_id = e.user_id AND c.bucket = :bucket"
            f") GROUP BY e.user_id"
        ),
        {"bucket": table},
    )


def init_db() -> None:
    """建立所有資料表（若不存在），並補上既有資料缺的欄位。"""
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401 匯入以註冊 ORM 模型

    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    names = set(insp.get_table_names())
    with engine.begin() as conn:
        if "expenses" in names:
            cols = {c["name"] for c in insp.get_columns("expenses")}
            if "kind" not in cols:
                conn.execute(text("ALTER TABLE expenses ADD COLUMN kind VARCHAR(16) DEFAULT 'expense'"))
            if "related_id" not in cols:
                conn.execute(text("ALTER TABLE expenses ADD COLUMN related_id INTEGER"))
        for table in _USER_SEQ_TABLES:
            if table not in names:
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if "seq" not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN seq INTEGER"))
            _backfill_user_seq(conn, table)
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_user_seq "
                    f"ON {table} (user_id, seq)"
                )
            )


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
