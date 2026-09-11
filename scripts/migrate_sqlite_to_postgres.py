"""把本機 SQLite (data/app.db) 遷移到 PostgreSQL。

用法：
  set DATABASE_URL=postgresql+psycopg2://user:pass@host:port/db
  python scripts/migrate_sqlite_to_postgres.py

預設只插入雲端尚不存在的資料（以「除 id 外」的業務鍵去重），
並保留雲端既有列。結束時印出兩邊筆數對照。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "app.db"

TABLES = (
    "schedules",
    "expenses",
    "notes",
    "stock_query_logs",
    "holdings",
)

# 用來判斷「同一筆」的欄位（不含 autoincrement id）
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
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


def sqlite_rows(table: str) -> list[dict]:
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f"SELECT * FROM [{table}]").fetchall()]
    con.close()
    for row in rows:
        for k, v in list(row.items()):
            if k.endswith("_at") or k in {"bought_at", "spent_at", "queried_at", "created_at", "start_at"}:
                row[k] = _parse_dt(v)
    return rows


def ensure_tables(pg: Engine) -> None:
    # 延遲 import，沿用專案 ORM 建表
    sys.path.insert(0, str(ROOT))
    os.environ["DATABASE_URL"] = _norm_url(os.environ["DATABASE_URL"])
    from app.db import Base  # noqa: WPS433
    import app.models  # noqa: F401,WPS433

    Base.metadata.create_all(bind=pg)


def existing_keys(pg: Engine, table: str) -> set[tuple]:
    keys = DEDUP_KEYS[table]
    cols = ", ".join(keys)
    with pg.connect() as conn:
        result = conn.execute(text(f"SELECT {cols} FROM {table}"))
        out: set[tuple] = set()
        for row in result:
            out.add(tuple(row))
        return out


def row_key(table: str, row: dict) -> tuple:
    return tuple(row.get(k) for k in DEDUP_KEYS[table])


def migrate_table(pg: Engine, table: str) -> tuple[int, int, int]:
    src = sqlite_rows(table)
    have = existing_keys(pg, table)
    to_insert = [r for r in src if row_key(table, r) not in have]
    if not to_insert:
        return len(src), 0, len(have)

    # 不帶原本 id，讓 Postgres 自己配號，避免序列衝突
    cols = [c for c in to_insert[0].keys() if c != "id"]
    col_sql = ", ".join(cols)
    ph = ", ".join(f":{c}" for c in cols)
    sql = text(f"INSERT INTO {table} ({col_sql}) VALUES ({ph})")
    with pg.begin() as conn:
        for row in to_insert:
            payload = {c: row[c] for c in cols}
            conn.execute(sql, payload)
    return len(src), len(to_insert), len(have)


def count_table(pg: Engine, table: str) -> int:
    with pg.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def main() -> int:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        print("請先設定 DATABASE_URL（Postgres 連線字串）", file=sys.stderr)
        return 1
    if not SQLITE_PATH.exists():
        print(f"找不到 {SQLITE_PATH}", file=sys.stderr)
        return 1

    url = _norm_url(raw)
    # 不印出完整 URL（可能含密碼）
    safe = url.split("@")[-1] if "@" in url else "<local>"
    print(f"目標 Postgres host/db: {safe}")
    print(f"來源 SQLite: {SQLITE_PATH}")

    pg = create_engine(url)
    ensure_tables(pg)

    print("\n=== 遷移結果 ===")
    for table in TABLES:
        src_n, inserted, existed = migrate_table(pg, table)
        remote_n = count_table(pg, table)
        print(
            f"{table}: local={src_n} already_remote≈{existed} inserted={inserted} remote_now={remote_n}"
        )

    token = ROOT / "data" / "google_calendar_token.json"
    print("\n=== 非 DB、需另行處理 ===")
    print(
        f"google_calendar_token.json: {'本機有檔' if token.exists() else '無'} "
        "→ 雲端應已用 /oauth/google/start 重新授權，不必搬檔"
    )
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
