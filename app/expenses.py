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
    cat = classify_category(note, category)
    when = spent_at or datetime.now()
    item = Expense(
        user_id=user_id,
        amount=float(amount),
        category=cat,
        note=note or None,
        spent_at=when,
    )
    session.add(item)
    session.flush()
    weekday = "一二三四五六日"[when.weekday()]
    when_label = f"{when.strftime('%Y/%m/%d')}（週{weekday}）"
    message = (
        f"已記帳 #{item.id}\n"
        f"金額：${amount:,.0f}\n"
        f"類別：{cat}\n"
        f"項目：{note or '（無備註）'}\n"
        f"時間：{when_label}"
    )
    return {
        "id": item.id,
        "amount": float(amount),
        "category": cat,
        "note": note or "（無備註）",
        "when_label": when_label,
        "message": message,
    }


def delete_expense_record(session: Session, user_id: str, expense_id: int) -> str:
    """依編號刪除一筆記帳（只能刪自己的）。"""
    item = session.get(Expense, expense_id)
    if not item or item.user_id != user_id:
        return f"找不到記帳編號 #{expense_id}，請先查「今天花了多少」確認編號。"
    when = item.spent_at or item.created_at
    wd = _weekday_name(when) if when else "?"
    ts = when.strftime("%Y/%m/%d") if when else ""
    note = item.note or "（無備註）"
    summary = (
        f"🗑 已刪除記帳 #{item.id}\n"
        f"金額：${item.amount:,.0f}\n"
        f"類別：{item.category}\n"
        f"項目：{note}\n"
        f"時間：{ts}（週{wd}）"
    )
    session.delete(item)
    session.flush()
    return summary


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

    m_amt = re.search(
        r"(?:NT\$|\$|＄)\s*(\d{1,7}(?:\.\d{1,2})?)",
        work,
        re.IGNORECASE,
    )
    if not m_amt:
        m_amt = re.search(
            r"(\d{1,7}(?:\.\d{1,2})?)\s*(元|塊)",
            work,
        )
    if not m_amt:
        for m in re.finditer(r"(\d{1,7}(?:\.\d{1,2})?)", work):
            left = work[m.start() - 1] if m.start() > 0 else ""
            right = work[m.end()] if m.end() < len(work) else ""
            if left in ("/", "-") or right in ("/", "-"):
                continue
            m_amt = m
            break
    if not m_amt:
        return None
    amount = float(m_amt.group(1))

    note = (work[: m_amt.start()] + " " + work[m_amt.end() :]).strip()
    note = re.sub(r"^(幫我)?(記帳|記錄|記下|記一下|記了?)\s*", "", note).strip(" ，,。/\\")
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
    """同一行多筆：停車費 490 停車費 150 → 切開。"""
    raw = (line or "").strip()
    if not raw:
        return []
    # 模式：非數字片段 + 金額（可帶元/塊/$）
    pairs = list(
        re.finditer(
            r"([^\d\r\n]{1,40}?)\s*(?:NT\$|\$|＄)?\s*(\d{1,7}(?:\.\d{1,2})?)\s*(?:元|塊)?",
            raw,
            re.IGNORECASE,
        )
    )
    if len(pairs) <= 1:
        return [raw]
    chunks: list[str] = []
    for m in pairs:
        note = m.group(1).strip(" ，,。;；/|")
        amt = m.group(2)
        if not note or re.fullmatch(r"[\s/.\-]*", note):
            continue
        chunks.append(f"{note} {amt}")
    return chunks if len(chunks) >= 2 else [raw]


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

    lines = [ln.strip() for ln in re.split(r"[\r\n]+", raw) if ln.strip()]
    shared_date: Optional[datetime] = None
    body_lines: list[str] = []

    for ln in lines:
        only_date = _parse_date_only_line(ln)
        if only_date is not None and not body_lines:
            shared_date = only_date
            continue
        body_lines.append(ln)

    if not body_lines:
        return []

    # 展開同一行多筆
    expanded: list[str] = []
    for ln in body_lines:
        expanded.extend(_split_inline_expense_chunks(ln))

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

    checks = (
        (r"今天|今日|本日|每天", "今天"),
        (r"昨天|昨日", "昨天"),
        (r"總共|全部|累計|總計|至今", "總共"),
        (r"今年|本年|當年|每年", "今年"),
        (r"本月|這個月|當月|每月", "本月"),
        (r"\d{1,2}[/-]\d{1,2}|\d{4}[-/]\d{1,2}[-/]\d{1,2}", None),  # group 0
        (r"\d{1,2}\s*月|[一二三四五六七八九十兩]{1,3}\s*月", None),
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
        lines.append(f"#{r.id} [{ts} 週{wd}] {r.category} ${r.amount:,.0f}  {note}")
    return lines


def _resolve_period(period: str) -> tuple[datetime, datetime, str]:
    now = datetime.now()
    p = (period or "month").strip()

    if p in ("day", "今天", "今日", "本日", "每天"):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), f"今天 {start.strftime('%Y/%m/%d')}（週{_weekday_name(start)}）"

    if p in ("yesterday", "昨天", "昨日"):
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), f"昨天 {start.strftime('%Y/%m/%d')}（週{_weekday_name(start)}）"

    if p in ("total", "全部", "總共", "累計", "所有", "總計"):
        return datetime(1970, 1, 1), datetime(2100, 1, 1), "全部期間"

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

    if p in ("year", "今年", "本年", "當年", "每年"):
        return datetime(now.year, 1, 1), datetime(now.year + 1, 1, 1), f"{now.year} 年"

    d = parse_user_date(p)
    if d:
        start = d.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), start.strftime("%Y/%m/%d") + f"（週{_weekday_name(start)}）"

    # 預設本月
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = calendar.monthrange(now.year, now.month)[1]
    return start, start.replace(day=last) + timedelta(days=1), f"{now.year}/{now.month:02d}"


def list_expenses_on_date(session: Session, user_id: str, date_str: str) -> str:
    start, end, label = _resolve_period(date_str)
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
    total = sum(r.amount for r in rows)
    lines = [f"📒 {label} 共 {len(rows)} 筆，合計 ${total:,.0f}", "—" * 8]
    lines.extend(_format_expense_lines(rows))
    return "\n".join(lines)


def summarize_expenses_period(session: Session, user_id: str, period: str = "month") -> str:
    start, end, label = _resolve_period(period)
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

    total = sum(r.amount for r in rows)
    by_cat: dict[str, float] = defaultdict(float)
    for r in rows:
        by_cat[r.category] += r.amount

    lines = [f"📒 {label} 花費統計", f"合計：${total:,.0f}（{len(rows)} 筆）", "—" * 8, "【類別】"]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"・{cat}：${amt:,.0f}（{amt / total * 100:.1f}%）")
    lines.append("—" * 8)
    lines.append("【細項】")
    lines.extend(_format_expense_lines(rows[:50]))
    if len(rows) > 50:
        lines.append(f"…其餘 {len(rows) - 50} 筆省略")
    return "\n".join(lines)


def category_breakdown(
    session: Session, user_id: str, period: str = "month"
) -> tuple[str, dict[str, float], str]:
    start, end, label = _resolve_period(period)
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
    by_cat: dict[str, float] = defaultdict(float)
    for r in rows:
        by_cat[r.category] += r.amount
    total = sum(by_cat.values())
    if not by_cat:
        return f"📒 {label} 沒有記帳資料，無法產生圓餅圖。", {}, label
    lines = [f"📒 {label} 消費類別占比（合計 ${total:,.0f}）"]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"・{cat}：${amt:,.0f}（{amt / total * 100:.1f}%）")
    return "\n".join(lines), dict(by_cat), label


def make_category_pie_chart(by_cat: dict[str, float], label: str) -> Optional[str]:
    if not by_cat:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = [
        "Microsoft JhengHei",
        "Microsoft YaHei",
        "Arial Unicode MS",
        "sans-serif",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    items = sorted(by_cat.items(), key=lambda x: -x[1])
    labels = [k for k, _ in items]
    sizes = [v for _, v in items]
    colors = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#B07AA1"]

    fig, ax = plt.subplots(figsize=(6, 6), dpi=140)
    _wedges, _texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        autopct=lambda p: f"{p:.1f}%" if p >= 3 else "",
        startangle=90,
        colors=colors[: len(sizes)],
        textprops={"fontsize": 11},
    )
    for t in autotexts:
        t.set_fontsize(10)
    ax.set_title(f"{label} 消費類別占比", fontsize=14, pad=16)
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
    start, end, label = _resolve_period(period)
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

    total = float(sum(r.amount for r in rows))
    by_cat_map: dict[str, float] = defaultdict(float)
    for r in rows:
        by_cat_map[r.category] += r.amount
    by_cat = sorted(by_cat_map.items(), key=lambda x: -x[1])
    detail_lines = _format_expense_lines(rows)

    text_lines = [
        f"📒 {label} 花費報告",
        f"合計：${total:,.0f}（{len(rows)} 筆）",
        "—" * 8,
        "【類別】",
    ]
    for cat, amt in by_cat:
        text_lines.append(f"・{cat}：${amt:,.0f}（{amt / total * 100:.1f}%）")
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

