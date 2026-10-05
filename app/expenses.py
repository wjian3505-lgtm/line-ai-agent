"""記帳：自動分類、查詢細項、日/月/年/總計、類別圓餅圖。"""
from __future__ import annotations

import calendar
import os
import re
import uuid
from contextvars import ContextVar
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .calendar_google import parse_user_date, parse_user_month, parse_user_year
from .config import settings
from .models import Expense
from .user_seq import find_by_seq, next_seq, show_no

CATEGORIES = ("伙食", "購物", "交通", "娛樂", "投資", "其他")
_pending_images: ContextVar[list] = ContextVar("expense_pending_images", default=None)


def reset_pending_images() -> None:
    _pending_images.set([])


def get_pending_images() -> list:
    return list(_pending_images.get() or [])


def _queue_image(path: str) -> None:
    lst = _pending_images.get()
    if lst is None:
        lst = []
        _pending_images.set(lst)
    lst.append(path)

_CATEGORY_KEYWORDS = {
    "伙食": [
        "早餐", "午餐", "晚餐", "消夜", "宵夜", "吃飯", "用餐", "便當", "麵", "飯",
        "咖啡", "飲料", "麥當勞", "肯德基", "星巴克", "小吃", "火鍋", "燒烤",
        "居酒屋", "餐廳", "食堂", "早餐店", "超商", "7-11", "全家", "萊爾富",
        "美食", "外送", "ubereats", "foodpanda", "飲料店", "手搖",
    ],
    "購物": [
        "購物", "買東西", "衣服", "鞋子", "包包", "配件", "商場", "百貨", "蝦皮",
        "momo", "pchome", "amazon", "全聯", "家樂福", "好市多", "costco", "超市",
        "日用品", "洗髮", "牙膏", "網購",
    ],
    "交通": [
        "捷運", "公車", "高鐵", "台鐵", "火車", "計程車", "uber", "加油", "停車",
        "油錢", "機票", "飛機", "悠遊卡", "一卡通", "客運", "機車", "汽車",
        "youbike", "交通", "計程車費",
    ],
    "娛樂": [
        "電影", "演唱會", "ktv", "遊戲", "娛樂", "旅遊", "住宿", "飯店", "門票",
        "展覽", "遊樂園", "酒吧", "健身", "健身房", "按摩", "netflix", "spotify",
        "訂閱",
    ],
    "投資": [
        "股票", "買股", "基金", "投資", "手續費", "匯費", "加密", "比特幣",
        "債券", "etf", "儲蓄險", "定存",
    ],
}


def classify_category(note: str, category: str = "") -> str:
    if category and category in CATEGORIES:
        return category
    text = (note or "").lower()
    for cat, words in _CATEGORY_KEYWORDS.items():
        for w in words:
            if w.lower() in text:
                return cat
    return "其他"


def add_expense_record(
    session: Session,
    user_id: str,
    amount: float,
    note: str = "",
    category: str = "",
    spent_at: Optional[datetime] = None,
) -> str:
    data = record_expense(
        session, user_id, amount=amount, note=note, category=category, spent_at=spent_at
    )
    if data.get("error"):
        return data["error"]
    return data["message"]


def record_expense(
    session: Session,
    user_id: str,
    amount: float,
    note: str = "",
    category: str = "",
    spent_at: Optional[datetime] = None,
) -> dict:
    if amount <= 0:
        return {"error": "金額必須大於 0。"}
    as_income = _text_has_income_word(note)
    cat = INCOME_CATEGORY if as_income else classify_category(note, category)
    when = spent_at or datetime.now()
    item = Expense(
        user_id=user_id,
        seq=next_seq(session, Expense, user_id),
        amount=float(amount),
        category=cat,
        note=note or None,
        spent_at=when,
        kind="income" if as_income else "expense",
    )
    session.add(item)
    session.flush()
    weekday = "一二三四五六日"[when.weekday()]
    when_label = f"{when.strftime('%Y/%m/%d')}（週{weekday}）"
    headline = "已記收入" if as_income else "已記帳"
    message = (
        f"{headline} #{show_no(item)}\n"
        f"金額：${amount:,.0f}\n"
        f"類別：{cat}\n"
        f"項目：{note or '（無備註）'}\n"
        f"時間：{when_label}"
    )
    return {
        "id": show_no(item),
        "amount": float(amount),
        "category": cat,
        "note": note or "（無備註）",
        "when_label": when_label,
        "message": message,
    }


def delete_expense_record(session: Session, user_id: str, expense_id: int) -> str:
    """依編號刪除一筆記帳（只能刪自己的）。"""
    item = find_by_seq(session, Expense, user_id, expense_id)
    if not item:
        return f"找不到記帳編號 #{expense_id}，請先查「今天花了多少」確認編號。"
    when = item.spent_at or item.created_at
    wd = _weekday_name(when) if when else "?"
    ts = when.strftime("%Y/%m/%d") if when else ""
    note = item.note or "（無備註）"
    summary = (
        f"🗑 已刪除記帳 #{show_no(item)}\n"
        f"金額：${item.amount:,.0f}\n"
        f"類別：{item.category}\n"
        f"項目：{note}\n"
        f"時間：{ts}（週{wd}）"
    )
    session.delete(item)
    session.flush()
    return summary


REFUND_WORDS = r"收回|退回|退我|還錢|還我|補回|轉回|匯回|找我|AA|分攤"
INCOME_WORDS = r"收入|進帳|入帳|零用錢|紅包|薪水|薪資|獎金|收到錢"
INCOME_CATEGORY = "收入"


def looks_like_refund(text: str) -> bool:
    """收回某一筆：要有收回類用詞，且有 #編號，或同時有日期與項目。"""
    t = (text or "").strip()
    if not t or not re.search(REFUND_WORDS, t, re.IGNORECASE):
        return False
    if re.search(r"[#＃]\s*\d+", t):
        return True
    has_date = bool(
        re.search(
            r"\d{1,2}[/-]\d{1,2}|\d{4}[-/]\d{1,2}[-/]\d{1,2}|今天|今日|昨天|昨日",
            t,
        )
    )
    stripped = re.sub(REFUND_WORDS, " ", t, flags=re.IGNORECASE)
    stripped = re.sub(r"[#＃]\s*\d+|\d{1,7}(?:\.\d{1,2})?|(元|塊|今天|今日|昨天|昨日)", " ", stripped)
    has_item = bool(re.search(r"[一-龥]{2,}", stripped))
    return has_date and has_item


_CN_NUM = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUM_CHARS = "".join(_CN_NUM)
_CN_UNIT_CHARS = "十百千萬億"


def _parse_chinese_amount(token: str) -> Optional[int]:
    """三百 → 300、十五 → 15、三千五百 → 3500。認不出就回傳 None。"""
    if not token:
        return None
    total = 0
    section = 0
    number = 0
    for ch in token:
        if ch in _CN_NUM:
            number = _CN_NUM[ch]
            continue
        unit = {"十": 10, "百": 100, "千": 1000}.get(ch)
        if unit:
            section += (number or 1) * unit
            number = 0
            continue
        if ch in ("萬", "億"):
            group = section + number
            total += (group or 1) * (10000 if ch == "萬" else 100000000)
            section = 0
            number = 0
            continue
        return None
    value = total + section + number
    if value <= 0:
        return None
    return value


def _money_spans(text: str) -> list[tuple[int, int, float]]:
    """句子裡的金額位置。略過日期、#編號，以及「週五」「十月」這種字。"""
    raw = text or ""
    spans: list[tuple[int, int, float]] = []
    for m in re.finditer(r"(\d{1,7}(?:\.\d{1,2})?)", raw):
        left = raw[m.start() - 1] if m.start() else ""
        right = raw[m.end()] if m.end() < len(raw) else ""
        if (left and left in "/-#＃") or (right and right in "/-月年號日"):
            continue
        prefix = raw[max(0, m.start() - 3) : m.start()]
        if re.search(r"[#＃]\s*$", prefix):
            continue
        spans.append((m.start(), m.end(), float(m.group(1))))
    for m in re.finditer(rf"[{_CN_NUM_CHARS}{_CN_UNIT_CHARS}]+", raw):
        token = m.group()
        right = raw[m.end()] if m.end() < len(raw) else ""
        # 空字串 in 「月年」會成立，句尾的「三百」不能因此被略過。
        if right and right in "月年號日":
            continue
        has_unit = any(ch in token for ch in _CN_UNIT_CHARS)
        if not has_unit and right not in ("元", "塊"):
            continue
        value = _parse_chinese_amount(token)
        if value is None:
            continue
        spans.append((m.start(), m.end(), float(value)))
    spans.sort(key=lambda item: (item[0], item[1]))
    kept: list[tuple[int, int, float]] = []
    last_end = -1
    for start, end, amount in spans:
        if start < last_end:
            continue
        kept.append((start, end, amount))
        last_end = end
    return kept


def _text_has_income_word(text: str) -> bool:
    return bool(re.search(INCOME_WORDS, text or ""))


def looks_like_income(text: str) -> bool:
    """整句都是收入才算。夾著午餐、交通這類花費時改由記帳拆開，不整句當收入。"""
    t = (text or "").strip()
    if not t or looks_like_refund(t):
        return False
    if not _text_has_income_word(t):
        return False
    if _extract_money_amount(t) is None:
        return False
    chunks, _shared = _bookkeeping_chunks(t)
    for chunk in chunks:
        if _extract_money_amount(chunk) is None:
            continue
        if not _text_has_income_word(chunk):
            return False
    return True


def _extract_money_amount(text: str) -> Optional[float]:
    """第一個金額。略過日期裡的數字，以及 #編號。也認「三百」。"""
    spans = _money_spans(text or "")
    if not spans:
        return None
    return spans[0][2]


def _refund_item_hint(text: str) -> str:
    t = re.sub(REFUND_WORDS, " ", text or "", flags=re.IGNORECASE)
    t = re.sub(r"[#＃]\s*\d+", " ", t)
    t = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}", " ", t)
    t = re.sub(r"\d{1,7}(?:\.\d{1,2})?", " ", t)
    t = re.sub(r"(元|塊|今天|今日|昨天|昨日|這筆|該筆|記帳|幫我)", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,。")
    return t


def record_refund(session: Session, user_id: str, text: str) -> dict:
    """另記一筆負數，扣在原支出的同一天、同一類別。不改原紀錄。"""
    amount = _extract_money_amount(text)
    if amount is None or amount <= 0:
        return {"error": "請寫收回金額，例如：收回 #74 200。"}
    target = _find_refund_target(session, user_id, text)
    if isinstance(target, str):
        return {"error": target}
    already = _refunded_amount(session, user_id, target.id)
    remaining = float(target.amount) - already
    if amount > remaining + 1e-6:
        return {
            "error": f"#{show_no(target)} 最多還能收回 ${remaining:,.0f}，這次 {amount:,.0f} 超過了。"
        }
    note = f"收回#{show_no(target)} {target.note or ''}".strip()
    item = Expense(
        user_id=user_id,
        seq=next_seq(session, Expense, user_id),
        amount=-float(amount),
        category=target.category,
        note=note[:255],
        spent_at=target.spent_at,
        kind="refund",
        related_id=target.id,
    )
    session.add(item)
    session.flush()
    when = target.spent_at
    weekday = "一二三四五六日"[when.weekday()] if when else "?"
    when_label = when.strftime("%Y/%m/%d") + f"（週{weekday}）" if when else ""
    message = (
        f"已收回 #{show_no(item)}\n"
        f"對應：#{show_no(target)} {target.note or ''}\n"
        f"收回：${amount:,.0f}\n"
        f"類別：{target.category}\n"
        f"日期：{when_label}\n"
        f"這筆剩餘：${remaining - amount:,.0f}"
    )
    return {
        "id": show_no(item),
        "amount": -float(amount),
        "category": target.category,
        "note": note,
        "when_label": when_label,
        "message": message,
    }


def record_income(session: Session, user_id: str, text: str) -> dict:
    """不對到某一筆花費的收入，例如零用錢、薪水。"""
    amount = _extract_money_amount(text)
    if amount is None or amount <= 0:
        return {"error": "請寫收入金額，例如：收入 200、零用錢 200。"}
    note = _income_note(text)
    spent_at = _income_spent_at(text)
    item = Expense(
        user_id=user_id,
        seq=next_seq(session, Expense, user_id),
        amount=float(amount),
        category=INCOME_CATEGORY,
        note=note[:255],
        spent_at=spent_at,
        kind="income",
        related_id=None,
    )
    session.add(item)
    session.flush()
    weekday = "一二三四五六日"[spent_at.weekday()]
    when_label = f"{spent_at.strftime('%Y/%m/%d')}（週{weekday}）"
    message = (
        f"已記收入 #{show_no(item)}\n"
        f"金額：${amount:,.0f}\n"
        f"項目：{note}\n"
        f"時間：{when_label}"
    )
    return {
        "id": show_no(item),
        "amount": float(amount),
        "category": INCOME_CATEGORY,
        "note": note,
        "when_label": when_label,
        "message": message,
    }


def _is_income_row(row: Expense) -> bool:
    return (getattr(row, "kind", None) or "") == "income" or row.category == INCOME_CATEGORY


def _find_refund_target(session: Session, user_id: str, text: str) -> Expense | str:
    m_id = re.search(r"[#＃]\s*(\d+)", text)
    if m_id:
        item = find_by_seq(session, Expense, user_id, int(m_id.group(1)))
        if not item:
            return f"找不到記帳 #{m_id.group(1)}。"
        if _is_income_row(item) or (getattr(item, "kind", None) or "expense") != "expense" or item.amount <= 0:
            return f"#{show_no(item)} 不是一筆花費，不能收回。"
        return item

    hint = _refund_item_hint(text)
    spent = _income_spent_at(text)
    start = spent.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    rows = (
        session.execute(
            select(Expense)
            .where(
                Expense.user_id == user_id,
                Expense.spent_at >= start,
                Expense.spent_at < end,
                Expense.amount > 0,
            )
            .order_by(Expense.id.asc())
        )
        .scalars()
        .all()
    )
    rows = [r for r in rows if not _is_income_row(r) and (getattr(r, "kind", None) or "expense") == "expense"]
    if hint:
        rows = [r for r in rows if hint in (r.note or "")]
    if not rows:
        return "找不到要收回的那一筆。請改成「收回 #編號 金額」。"
    if len(rows) > 1:
        shown = "、".join(f"#{show_no(r)} {r.note or ''} ${r.amount:,.0f}" for r in rows[:6])
        return f"當天有多筆，請指定編號。例如：收回 #{show_no(rows[0])} 。\n{shown}"
    return rows[0]


def _refunded_amount(session: Session, user_id: str, expense_id: int) -> float:
    rows = (
        session.execute(
            select(Expense).where(
                Expense.user_id == user_id,
                Expense.related_id == expense_id,
                Expense.kind == "refund",
            )
        )
        .scalars()
        .all()
    )
    return float(sum(-r.amount for r in rows if r.amount < 0))


def _income_note(text: str) -> str:
    t = re.sub(INCOME_WORDS, " ", text or "")
    t = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}", " ", t)
    t = re.sub(r"\d{1,7}(?:\.\d{1,2})?", " ", t)
    t = re.sub(rf"[{_CN_NUM_CHARS}{_CN_UNIT_CHARS}]+", " ", t)
    t = re.sub(r"(元|塊|今天|今日|昨天|昨日|幫我|記下|記錄|記帳)", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,。")
    if t:
        return t
    m = re.search(INCOME_WORDS, text or "")
    return m.group(0) if m else "收入"


def _income_spent_at(text: str) -> datetime:
    raw = text or ""
    if re.search(r"昨天|昨日", raw):
        return (datetime.now() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    if re.search(r"今天|今日", raw):
        return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2})", raw)
    if m:
        d = parse_user_date(m.group(1))
        if d:
            return d.replace(tzinfo=None, hour=12, minute=0, second=0, microsecond=0)
    return datetime.now().replace(microsecond=0)


def parse_expense_utterance(text: str) -> Optional[tuple[float, str, datetime]]:
    """解析單筆「今天早餐 100元」「8/5 晚餐 210元」→ (amount, note, spent_at)。"""
    items = parse_expense_utterances(text)
    if len(items) == 1:
        return items[0]
    # 多筆時單筆 API 回傳 None，請改用 parse_expense_utterances
    if len(items) > 1:
        return None
    return None


def _parse_one_expense_line(
    line: str,
    *,
    default_spent_at: Optional[datetime] = None,
) -> Optional[tuple[float, str, datetime]]:
    """解析單一記帳行（不含跨行）。"""
    raw = (line or "").strip()
    if not raw:
        return None
    if looks_like_refund(raw):
        return None
    if re.search(r"(行程|行事曆|股價|漲幅|跌幅|盈虧|報酬率|安排)", raw):
        return None
    if re.search(r"買", raw) and re.search(
        r"張|股|[A-Za-z]{0,2}\d{3,5}[A-Za-z]?", raw
    ):
        return None

    work = raw
    spent_at = default_spent_at or datetime.now()
    had_explicit_date = False

    if re.search(r"昨天|昨日", work):
        spent_at = datetime.now() - timedelta(days=1)
        had_explicit_date = True
    m_date = re.search(
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2})",
        work,
    )
    if m_date:
        d = parse_user_date(m_date.group(1))
        if d:
            spent_at = d.replace(tzinfo=None, hour=12, minute=0, second=0, microsecond=0)
            work = (work[: m_date.start()] + " " + work[m_date.end() :]).strip()
            had_explicit_date = True

    if re.search(r"(今天|今日)", work):
        spent_at = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        had_explicit_date = True
    work = re.sub(r"(今天|今日|昨天|昨日)", " ", work)
    work = re.sub(r"\s+", " ", work).strip(" ，,。")

    spans = _money_spans(work)
    if not spans:
        return None
    span_start, span_end, amount = spans[0]

    note = (work[:span_start] + " " + work[span_end:]).strip()
    note = re.sub(r"^(幫我)?(記帳|記錄|記下|記一下|記了?)\s*", "", note).strip(" ，,。/\\")
    note = re.sub(r"(^|\s)(元|塊)(?=\s|$)", " ", note)
    note = re.sub(r"(元|塊)\s*$", "", note).strip()
    note = re.sub(r"\s+", " ", note).strip(" ，,。/\\")
    if not note:
        note = "未命名支出"

    if note == "未命名支出" and not re.search(r"(元|塊|花|記帳|支出|\$)", raw):
        return None

    if default_spent_at is not None and not had_explicit_date:
        spent_at = default_spent_at

    return amount, note, spent_at


def _parse_date_only_line(line: str) -> Optional[datetime]:
    """整行只有日期 → 回傳當天中午；否則 None。"""
    raw = (line or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"(今天|今日)", raw):
        return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    if re.fullmatch(r"(昨天|昨日)", raw):
        return (datetime.now() - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
    m = re.fullmatch(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2})", raw)
    if not m:
        return None
    d = parse_user_date(m.group(1))
    if not d:
        return None
    return d.replace(tzinfo=None, hour=12, minute=0, second=0, microsecond=0)


def _split_inline_expense_chunks(line: str) -> list[str]:
    """同一行多筆：停車費 490 停車費 150、午餐 100 收入 200 → 切開。"""
    raw = (line or "").strip()
    if not raw:
        return []
    spans = _money_spans(raw)
    if len(spans) <= 1:
        return [raw]
    chunks: list[str] = []
    prev = 0
    for start, end, _amount in spans:
        piece = raw[prev:end].strip(" ，,。;；/|")
        prev = end
        if piece and not re.fullmatch(r"[\s/.\-]*", piece):
            chunks.append(piece)
    return chunks if len(chunks) >= 2 else [raw]


def _bookkeeping_chunks(text: str) -> tuple[list[str], Optional[datetime]]:
    """拆成一筆一筆的記帳短句，並帶出多行共用的日期。"""
    raw = (text or "").strip()
    if not raw:
        return [], None
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", raw) if ln.strip()]
    shared_date: Optional[datetime] = None
    body_lines: list[str] = []
    for ln in lines:
        only_date = _parse_date_only_line(ln)
        if only_date is not None and not body_lines:
            shared_date = only_date
            continue
        body_lines.append(ln)
    expanded: list[str] = []
    for ln in body_lines:
        expanded.extend(_split_inline_expense_chunks(ln))
    return expanded, shared_date


def parse_expense_utterances(text: str) -> list[tuple[float, str, datetime]]:
    """解析單筆或多筆記帳。

    支援：
    - 單行：今天早餐 100元
    - 多行（共用日期）：
        8/15
        停車費 490
        停車費 150
        加油 500
    - 同一行多筆：停車費 490 停車費 150 加油 500
    """
    raw = (text or "").strip()
    if not raw:
        return []
    if re.search(r"(行程|行事曆|股價|漲幅|跌幅|盈虧|報酬率)", raw):
        return []

    expanded, shared_date = _bookkeeping_chunks(raw)
    if not expanded:
        return []

    results: list[tuple[float, str, datetime]] = []
    for ln in expanded:
        parsed = _parse_one_expense_line(ln, default_spent_at=shared_date)
        if parsed:
            results.append(parsed)

    # 若切開後反而 0 筆，退回整段當單筆
    if not results and shared_date is None:
        one = _parse_one_expense_line(raw)
        if one:
            return [one]
    return results


def extract_period_hint(text: str, default: str = "month") -> str:
    """從句子抽出記帳期間提示（給 A 路徑用）。完整同義／解析交給 _resolve_period。"""
    t = (text or "").strip()
    if not t:
        return default

    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2))}"
    m = re.search(r"(今年|去年|明年|本年)\s*(\d{1,2})\s*月", t)
    if m:
        return m.group(0).replace(" ", "")
    m_md = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日號]", t)
    if m_md:
        return f"{int(m_md.group(1))}/{int(m_md.group(2))}"
    m_day = re.search(r"(?<!\d)(\d{1,2})\s*[日號]", t)
    if m_day and not re.search(r"\d{1,2}\s*月", t):
        return f"{datetime.now().month}/{int(m_day.group(1))}"

    checks = (
        (r"今天|今日|本日|每天", "今天"),
        (r"昨天|昨日", "昨天"),
        (r"前天", "前天"),
        (r"總共|全部|累計|總計|至今", "總共"),
        (r"上個月|上月", "上個月"),
        (r"下個月|下月", "下個月"),
        (r"本月|這個月|當月|每月", "本月"),
        (r"\d{1,2}[/-]\d{1,2}|\d{4}[-/]\d{1,2}[-/]\d{1,2}", None),  # group 0
        (r"\d{1,2}\s*月|[一二三四五六七八九十兩]{1,3}\s*月", None),
        (r"去年", "去年"),
        (r"明年", "明年"),
        (r"今年|本年|當年|每年", "今年"),
        (r"\d{4}\s*年?|\d{4}", None),
    )
    for pattern, fixed in checks:
        m = re.search(pattern, t)
        if not m:
            continue
        return fixed if fixed is not None else m.group(0)
    return default


def _weekday_name(dt: datetime) -> str:
    return "一二三四五六日"[dt.weekday()]


def _format_expense_lines(rows: list[Expense]) -> list[str]:
    lines = []
    for r in rows:
        when = r.spent_at or r.created_at
        wd = _weekday_name(when) if when else "?"
        ts = when.strftime("%m/%d") if when else ""
        note = r.note or ""
        amt = float(r.amount)
        money = f"-${abs(amt):,.0f}" if amt < 0 else f"${amt:,.0f}"
        lines.append(f"#{show_no(r)} [{ts} 週{wd}] {r.category} {money}  {note}")
    return lines


def _money_parts(rows: list[Expense]) -> tuple[list[Expense], list[Expense], float, float]:
    """花費（含收回負數）與收入分開。沒有收入時，花費合計與舊版相同。"""
    spend = [r for r in rows if not _is_income_row(r)]
    income = [r for r in rows if _is_income_row(r)]
    spend_total = float(sum(r.amount for r in spend))
    income_total = float(sum(r.amount for r in income))
    return spend, income, spend_total, income_total


def resolve_expense_period(text: str, default: str | None = None) -> tuple[datetime, datetime, str]:
    """從整句算出記帳區間。8-9月花費會含 8 月和 9 月，不會只剩後面那個月。"""
    span = _expense_range_span(text)
    if span:
        return span
    if default is None:
        default = "今天" if re.search(r"今天|今日", text or "") else "本月"
    return _resolve_period(extract_period_hint(text, default=default))


def _expense_range_span(text: str) -> tuple[datetime, datetime, str] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    now = datetime.now()

    m_recent = re.search(
        r"(近|最近|過去|這)\s*([兩三四五六七八九十]|\d{1,2})\s*個?\s*月",
        raw,
    )
    if m_recent:
        token = m_recent.group(2)
        n = int(token) if token.isdigit() else {"兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(token, 0)
        if 1 <= n <= 24:
            start_month = now.month - (n - 1)
            start_year = now.year
            while start_month <= 0:
                start_month += 12
                start_year -= 1
            start = datetime(start_year, start_month, 1)
            end = datetime(now.year + (1 if now.month == 12 else 0), 1 if now.month == 12 else now.month + 1, 1)
            return start, end, f"{start_year}/{start_month:02d}–{now.year}/{now.month:02d}"

    year = now.year
    if re.search(r"去年", raw):
        year -= 1
    elif re.search(r"明年", raw):
        year += 1
    if re.search(r"上半年", raw):
        return datetime(year, 1, 1), datetime(year, 7, 1), f"{year} 上半年"
    if re.search(r"下半年", raw):
        return datetime(year, 7, 1), datetime(year + 1, 1, 1), f"{year} 下半年"
    m_q = re.search(r"Q([1-4])|第([一二三四1-4])季", raw, re.IGNORECASE)
    if m_q:
        qtoken = m_q.group(1) or m_q.group(2)
        q = int(qtoken) if qtoken.isdigit() else {"一": 1, "二": 2, "三": 3, "四": 4}[qtoken]
        start_m = (q - 1) * 3 + 1
        end_m = start_m + 3
        end_y = year + 1 if end_m == 13 else year
        end_m = 1 if end_m == 13 else end_m
        return datetime(year, start_m, 1), datetime(end_y, end_m, 1), f"{year} Q{q}"

    if not re.search(r"(到|至|~|～|-|—|–)", raw):
        return None
    m_days = re.search(
        r"(\d{1,2})[/-](\d{1,2})\s*[-~～到至]\s*(\d{1,2})[/-](\d{1,2})",
        raw,
    )
    if m_days:
        y = now.year
        try:
            start = datetime(y, int(m_days.group(1)), int(m_days.group(2)))
            end_day = datetime(y, int(m_days.group(3)), int(m_days.group(4)))
        except ValueError:
            start = end_day = None
        if start and end_day:
            if end_day < start:
                end_day = datetime(y + 1, end_day.month, end_day.day)
            end = end_day + timedelta(days=1)
            return start, end, f"{start.strftime('%m/%d')}–{end_day.strftime('%m/%d')}"
    from .calendar_google import parse_user_month_range

    months = parse_user_month_range(raw)
    if months:
        (y1, m1), (y2, m2) = months
        start = datetime(y1, m1, 1)
        end = datetime(y2 + 1, 1, 1) if m2 == 12 else datetime(y2, m2 + 1, 1)
        return start, end, f"{y1}/{m1:02d}–{y2}/{m2:02d}"
    return None


def _resolve_period(period: str) -> tuple[datetime, datetime, str]:
    now = datetime.now()
    p = (period or "month").strip()
    ranged = _expense_range_span(p)
    if ranged:
        return ranged

    if p in ("day", "今天", "今日", "本日", "每天"):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), f"今天 {start.strftime('%Y/%m/%d')}（週{_weekday_name(start)}）"

    if p in ("yesterday", "昨天", "昨日"):
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), f"昨天 {start.strftime('%Y/%m/%d')}（週{_weekday_name(start)}）"

    if p == "前天":
        start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), f"前天 {start.strftime('%Y/%m/%d')}（週{_weekday_name(start)}）"

    if p in ("total", "全部", "總共", "累計", "所有", "總計", "至今"):
        return datetime(1970, 1, 1), datetime(2100, 1, 1), "全部期間"

    m_rel = re.fullmatch(r"(今年|去年|明年|本年)(\d{1,2})月", p)
    if m_rel:
        year = now.year
        if m_rel.group(1) == "去年":
            year -= 1
        elif m_rel.group(1) == "明年":
            year += 1
        month = int(m_rel.group(2))
        if 1 <= month <= 12:
            start = datetime(year, month, 1)
            last = calendar.monthrange(year, month)[1]
            return start, datetime(year, month, last) + timedelta(days=1), f"{year}/{month:02d}"

    m = parse_user_month(p)
    if m and (
        p in ("month", "本月", "這個月", "當月", "每月")
        or re.search(r"月", p)
        or re.fullmatch(r"\d{4}[-/]\d{1,2}", p)
    ):
        y, mo = m
        start = datetime(y, mo, 1)
        last = calendar.monthrange(y, mo)[1]
        return start, datetime(y, mo, last) + timedelta(days=1), f"{y}/{mo:02d}"

    if p in ("month", "本月", "這個月", "當月", "每月"):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last = calendar.monthrange(now.year, now.month)[1]
        return start, start.replace(day=last) + timedelta(days=1), f"{now.year}/{now.month:02d}"

    y = parse_user_year(p)
    if y and (p in ("year", "今年", "本年", "當年", "每年") or re.search(r"年", p) or re.fullmatch(r"\d{4}", p)):
        return datetime(y, 1, 1), datetime(y + 1, 1, 1), f"{y} 年"

    if p in ("year", "今年", "本年", "當年", "每年", "去年", "明年"):
        year = parse_user_year(p) or now.year
        return datetime(year, 1, 1), datetime(year + 1, 1, 1), f"{year} 年"

    d = parse_user_date(p)
    if d:
        start = d.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), start.strftime("%Y/%m/%d") + f"（週{_weekday_name(start)}）"

    # 預設本月
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = calendar.monthrange(now.year, now.month)[1]
    return start, start.replace(day=last) + timedelta(days=1), f"{now.year}/{now.month:02d}"


def list_expenses_on_date(session: Session, user_id: str, date_str: str) -> str:
    start, end, label = resolve_expense_period(date_str)
    rows = (
        session.execute(
            select(Expense)
            .where(
                Expense.user_id == user_id,
                Expense.spent_at >= start,
                Expense.spent_at < end,
            )
            .order_by(Expense.spent_at.asc(), Expense.id.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return f"📒 {label}\n這段期間沒有記帳紀錄。"
    _spend, _income, spend_total, income_total = _money_parts(rows)
    lines = [f"📒 {label} 共 {len(rows)} 筆，花費 ${spend_total:,.0f}"]
    if income_total:
        lines.append(f"收入 ${income_total:,.0f}，淨支出 ${spend_total - income_total:,.0f}")
    lines.append("—" * 8)
    lines.extend(_format_expense_lines(rows))
    return "\n".join(lines)


def summarize_expenses_period(session: Session, user_id: str, period: str = "month") -> str:
    start, end, label = resolve_expense_period(period)
    rows = (
        session.execute(
            select(Expense)
            .where(
                Expense.user_id == user_id,
                Expense.spent_at >= start,
                Expense.spent_at < end,
            )
            .order_by(Expense.spent_at.asc(), Expense.id.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return f"📒 {label}\n這段期間沒有記帳紀錄。"

    spend_rows, _income_rows, spend_total, income_total = _money_parts(rows)
    by_cat: dict[str, float] = defaultdict(float)
    for r in spend_rows:
        by_cat[r.category] += r.amount
    by_cat = {k: v for k, v in by_cat.items() if v > 0}

    lines = [f"📒 {label} 花費統計", f"合計：${spend_total:,.0f}（{len(rows)} 筆）", "—" * 8, "【類別】"]
    if income_total:
        lines.insert(2, f"收入：${income_total:,.0f}")
        lines.insert(3, f"淨支出：${spend_total - income_total:,.0f}")
    base = spend_total if spend_total else 1
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"・{cat}：${amt:,.0f}（{amt / base * 100:.1f}%）")
    lines.append("—" * 8)
    lines.append("【細項】")
    lines.extend(_format_expense_lines(rows[:50]))
    if len(rows) > 50:
        lines.append(f"…其餘 {len(rows) - 50} 筆省略")
    return "\n".join(lines)


def category_breakdown(
    session: Session, user_id: str, period: str = "month"
) -> tuple[str, dict[str, float], str]:
    start, end, label = resolve_expense_period(period)
    rows = (
        session.execute(
            select(Expense).where(
                Expense.user_id == user_id,
                Expense.spent_at >= start,
                Expense.spent_at < end,
            )
        )
        .scalars()
        .all()
    )
    spend_rows, _income_rows, spend_total, _income_total = _money_parts(rows)
    by_cat: dict[str, float] = defaultdict(float)
    for r in spend_rows:
        by_cat[r.category] += r.amount
    by_cat = {k: v for k, v in by_cat.items() if v > 0}
    total = spend_total
    if not by_cat:
        return f"📒 {label} 沒有記帳資料，無法產生圓餅圖。", {}, label
    base = total if total > 0 else sum(by_cat.values())
    lines = [f"📒 {label} 消費類別占比（合計 ${total:,.0f}）"]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"・{cat}：${amt:,.0f}（{amt / base * 100:.1f}%）")
    return "\n".join(lines), dict(by_cat), label


def _cjk_font_properties():
    """挑選可用的繁中／CJK 字型（本機 Windows 或 Linux 容器）。"""
    from matplotlib import font_manager
    from matplotlib.font_manager import FontProperties

    preferred_names = (
        "Noto Sans CJK TC",
        "Noto Sans CJK JP",
        "Noto Sans TC",
        "Noto Sans CJK",
        "WenQuanYi Zen Hei",
        "WenQuanYi Micro Hei",
        "Microsoft JhengHei",
        "Microsoft YaHei",
        "PingFang TC",
        "Arial Unicode MS",
    )
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in preferred_names:
        if name in available:
            return FontProperties(family=name)

    # 依常見檔名路徑搜尋（Docker / fonts-noto-cjk）
    path_globs = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK*.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK*.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    )
    import glob

    for pattern in path_globs:
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                try:
                    font_manager.fontManager.addfont(path)
                except Exception:  # noqa: BLE001
                    pass
                return FontProperties(fname=path)
    return FontProperties()


def make_category_pie_chart(by_cat: dict[str, float], label: str) -> Optional[str]:
    if not by_cat:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font_prop = _cjk_font_properties()
    family = font_prop.get_name()
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = [
        family,
        "Noto Sans CJK TC",
        "WenQuanYi Zen Hei",
        "Microsoft JhengHei",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    items = sorted(by_cat.items(), key=lambda x: -x[1])
    labels = [k for k, _ in items]
    sizes = [v for _, v in items]
    colors = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#B07AA1"]

    fig, ax = plt.subplots(figsize=(6, 6), dpi=140)
    _wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        autopct=lambda p: f"{p:.1f}%" if p >= 3 else "",
        startangle=90,
        colors=colors[: len(sizes)],
        textprops={"fontsize": 11, "fontproperties": font_prop},
    )
    for t in texts:
        t.set_fontproperties(font_prop)
        t.set_fontsize(11)
    for t in autotexts:
        t.set_fontsize(10)
    ax.set_title(
        f"{label} 消費類別占比",
        fontsize=14,
        pad=16,
        fontproperties=font_prop,
    )
    ax.axis("equal")

    out_dir = os.path.join("data", "charts")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"expense_pie_{uuid.uuid4().hex[:10]}.png")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def build_expense_full_report(
    session: Session, user_id: str, period: str = "month"
) -> dict:
    """統一花費報告：合計＋類別＋完整明細文字＋圓餅圖路徑。

    回傳：
      {
        "empty": bool,
        "label": str,
        "total": float,
        "count": int,
        "by_cat": list[tuple[str,float]],
        "detail_lines": list[str],
        "text": str,
        "chart_path": str | None,
      }
    """
    start, end, label = resolve_expense_period(period)
    rows = (
        session.execute(
            select(Expense)
            .where(
                Expense.user_id == user_id,
                Expense.spent_at >= start,
                Expense.spent_at < end,
            )
            .order_by(Expense.spent_at.asc(), Expense.id.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return {
            "empty": True,
            "label": label,
            "total": 0.0,
            "count": 0,
            "by_cat": [],
            "detail_lines": [],
            "text": f"📒 {label}\n這段期間沒有記帳紀錄。",
            "chart_path": None,
        }

    _spend_rows, _income_rows, spend_total, income_total = _money_parts(rows)
    by_cat_map: dict[str, float] = defaultdict(float)
    for r in _spend_rows:
        by_cat_map[r.category] += r.amount
    by_cat = sorted(((k, v) for k, v in by_cat_map.items() if v > 0), key=lambda x: -x[1])
    detail_lines = _format_expense_lines(rows)
    total = spend_total
    net = spend_total - income_total

    text_lines = [
        f"📒 {label} 花費報告",
        f"合計：${spend_total:,.0f}（{len(rows)} 筆）",
    ]
    if income_total:
        text_lines.append(f"收入：${income_total:,.0f}")
        text_lines.append(f"淨支出：${net:,.0f}")
    text_lines.extend(["—" * 8, "【類別】"])
    base = spend_total if spend_total else 1
    for cat, amt in by_cat:
        text_lines.append(f"・{cat}：${amt:,.0f}（{amt / base * 100:.1f}%）")
    text_lines.append("—" * 8)
    text_lines.append("【細項】")
    text_lines.extend(detail_lines)

    chart_path = make_category_pie_chart(dict(by_cat), label)
    if chart_path:
        _queue_image(chart_path)

    return {
        "empty": False,
        "label": label,
        "total": total,
        "spend": spend_total,
        "income": income_total,
        "net": net,
        "count": len(rows),
        "by_cat": by_cat,
        "detail_lines": detail_lines,
        "text": "\n".join(text_lines),
        "chart_path": chart_path,
    }


def build_category_chart_reply(session: Session, user_id: str, period: str = "month") -> str:
    text, by_cat, label = category_breakdown(session, user_id, period)
    if not by_cat:
        return text
    path = make_category_pie_chart(by_cat, label)
    if path:
        _queue_image(path)
        return text + "\n\n（已附上圓餅圖）"
    return text


def public_media_url(relative_path: str) -> str:
    base = (settings.public_base_url or "http://localhost:8080").rstrip("/")
    return f"{base}/media/charts/{os.path.basename(relative_path)}"

