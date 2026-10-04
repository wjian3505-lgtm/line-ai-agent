"""每個 LINE 帳號自己的編號。

資料庫 id 仍是整張表的流水號，畫面上的 #編號用 seq。
同一個人的號碼各自往下加，刪掉的號碼不回收。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import UserSeqCounter


def next_seq(session: Session, model, user_id: str) -> int:
    bucket = model.__tablename__
    row = session.scalar(
        select(UserSeqCounter).where(
            UserSeqCounter.user_id == user_id,
            UserSeqCounter.bucket == bucket,
        )
    )
    if row is None:
        current = session.scalar(select(func.max(model.seq)).where(model.user_id == user_id))
        row = UserSeqCounter(user_id=user_id, bucket=bucket, last_seq=int(current or 0))
        session.add(row)
        session.flush()
    row.last_seq = int(row.last_seq) + 1
    session.flush()
    return int(row.last_seq)


def find_by_seq(session: Session, model, user_id: str, seq: int):
    return session.scalar(
        select(model).where(model.user_id == user_id, model.seq == int(seq))
    )


def show_no(row) -> int:
    seq = getattr(row, "seq", None)
    if seq is None:
        return int(row.id)
    return int(seq)
