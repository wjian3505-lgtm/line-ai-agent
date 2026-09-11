"""在 Zeabur 容器內執行：讀 stdin JSON，寫入目前的 DATABASE_URL。不依賴 app package。"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine

TABLES = (
    "schedules",
    "expenses",
    "notes",
    "stock_query_logs",
    "holdings",
)

DEDUP_KEYS: dict[str, tuple[str, ...]] = {
    "schedules": ("user_id", "title", "start_at", "location", "note", "done"),
    "expenses": ("user_id", "amount", "category", "note", "spent_at"),
    "notes": ("user_id", "content", "created_at"),
    "stock_query_logs": ("user_id", "symbol", "price", "queried_at"),
    "holdings": (
        "user_id",
        "symbol",
        "name",
        "buy_price",
        "quantity",
        "bought_at",
        "note",
        "closed",
    ),
}


def _parse_dt(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return v


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        return "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


def ensure_tables(pg: Engine) -> None:
    md = MetaData()
    Table(
        "schedules",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", String(64), index=True),
        Column("title", String(255)),
        Column("start_at", DateTime, nullable=True),
        Column("location", String(255), nullable=True),
        Column("note", Text, nullable=True),
        Column("done", Integer, default=0),
        Column("created_at", DateTime),
    )
    Table(
        "expenses",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", String(64), index=True),
        Column("amount", Float),
        Column("category", String(64)),
        Column("note", String(255), nullable=True),
        Column("spent_at", DateTime),
        Column("created_at", DateTime),
    )
    Table(
        "notes",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", String(64), index=True),
        Column("content", Text),
        Column("created_at", DateTime),
    )
    Table(
        "stock_query_logs",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", String(64), index=True),
        Column("symbol", String(16), index=True),
        Column("name", String(64), nullable=True),
        Column("price", Float, nullable=True),
        Column("queried_at", DateTime, index=True),
    )
    Table(
        "holdings",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", String(64), index=True),
        Column("symbol", String(16), index=True),
        Column("name", String(64), nullable=True),
        Column("buy_price", Float),
        Column("quantity", Float),
        Column("bought_at", DateTime, nullable=True),
        Column("note", String(255), nullable=True),
        Column("closed", Integer, default=0),
        Column("created_at", DateTime),
    )
    md.create_all(bind=pg)


def existing_keys(pg: Engine, table: str) -> set[tuple]:
    keys = DEDUP_KEYS[table]
    cols = ", ".join(keys)
    with pg.connect() as conn:
        return {tuple(row) for row in conn.execute(text(f"SELECT {cols} FROM {table}"))}


def migrate(pg: Engine, table: str, rows: list[dict]) -> tuple[int, int]:
    for row in rows:
        for k, v in list(row.items()):
            if k.endswith("_at") or k in {
                "bought_at",
                "spent_at",
                "queried_at",
                "created_at",
                "start_at",
            }:
                row[k] = _parse_dt(v)
    have = existing_keys(pg, table)
    to_insert = [
        r for r in rows if tuple(r.get(k) for k in DEDUP_KEYS[table]) not in have
    ]
    if not to_insert:
        return 0, len(have)
    cols = [c for c in to_insert[0].keys() if c != "id"]
    col_sql = ", ".join(cols)
    ph = ", ".join(f":{c}" for c in cols)
    sql = text(f"INSERT INTO {table} ({col_sql}) VALUES ({ph})")
    with pg.begin() as conn:
        for row in to_insert:
            conn.execute(sql, {c: row[c] for c in cols})
    return len(to_insert), len(have)


def main() -> int:
    raw = os.getenv("DATABASE_URL", "")
    url = _norm_url(raw)
    if not url or "${" in url or "sqlite" in url:
        print(f"BAD DATABASE_URL len={len(url)}", file=sys.stderr)
        return 2
    payload = json.load(sys.stdin)
    pg = create_engine(url)
    ensure_tables(pg)
    print("host", url.split("@")[-1] if "@" in url else "?")
    for table in TABLES:
        rows = payload.get(table, [])
        inserted, existed = migrate(pg, table, rows)
        with pg.connect() as conn:
            now = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        print(
            f"{table}: local={len(rows)} existed≈{existed} inserted={inserted} remote_now={now}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
