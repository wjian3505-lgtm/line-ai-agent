"""持股盈虧與查詢紀錄整理。"""
import re
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Holding, StockQueryLog, StockSale, StockSaleLot
from .stock import get_stock_quote, resolve_symbol
from .user_seq import find_by_seq, next_seq, show_no


def log_stock_query(
    session: Session,
    user_id: str,
    symbol: str,
    name: Optional[str] = None,
    price: Optional[float] = None,
) -> None:
    session.add(
        StockQueryLog(
            user_id=user_id,
            symbol=symbol,
            name=name,
            price=price,
            queried_at=datetime.now(),
        )
    )
    session.flush()


def summarize_today_queries(session: Session, user_id: str) -> str:
    """整理使用者今天查過的股票（去重，保留最後一次查詢價）。"""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    rows = (
        session.execute(
            select(StockQueryLog)
            .where(
                StockQueryLog.user_id == user_id,
                StockQueryLog.queried_at >= start,
                StockQueryLog.queried_at < end,
            )
            .order_by(StockQueryLog.queried_at.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return "今天還沒有查詢任何股票。"

    latest = {}
    order: List[str] = []
    for r in rows:
        if r.symbol not in latest:
            order.append(r.symbol)
        latest[r.symbol] = r

    lines = [f"📋 今天查詢過的股票（{len(order)} 檔）："]
    for i, sym in enumerate(order, 1):
        r = latest[sym]
        name = r.name or ""
        price = f"{r.price}" if r.price is not None else "-"
        ts = r.queried_at.strftime("%H:%M") if r.queried_at else ""
        lines.append(f"{i}. {sym} {name}  當時 {price}  （最後查詢 {ts}）")
    return "\n".join(lines)


def parse_stock_buy_utterance(text: str) -> Optional[tuple[str, float, float]]:
    """解析買股句 → (代號或名稱, 買價, 股數)。

    例：00981A 28塊 買2張／我買了欣興 844元 1張／買入 2330 @900 0.5張
    """
    import re

    raw = (text or "").strip()
    if not raw:
        return None
    if re.search(r"(早餐|午餐|晚餐|消夜|捷運|記帳|花了多少|行程|行事曆)", raw):
        return None
    if not re.search(r"買", raw):
        return None

    # 張／股（沒說預設 1 張）
    qty = 1000.0
    m_lot = re.search(r"(\d+(?:\.\d+)?)\s*張", raw)
    m_share = re.search(r"(\d+(?:\.\d+)?)\s*股", raw)
    if m_lot:
        qty = float(m_lot.group(1)) * 1000.0
    elif m_share:
        qty = float(m_share.group(1))

    # 價格：優先「28塊／28元／@28」
    m_price = re.search(
        r"(?:@|每股|單價)?\s*(\d+(?:\.\d+)?)\s*(?:元|塊)",
        raw,
    )
    if not m_price:
        m_price = re.search(r"@\s*(\d+(?:\.\d+)?)", raw)
    if not m_price:
        return None
    buy_price = float(m_price.group(1))

    # 先拿掉價格／張數，避免把 844元 的 844 誤認成代號
    work = raw
    work = re.sub(r"(?:@|每股|單價)?\s*\d+(?:\.\d+)?\s*(?:元|塊)", " ", work)
    work = re.sub(r"@\s*\d+(?:\.\d+)?", " ", work)
    work = re.sub(r"\d+(?:\.\d+)?\s*張", " ", work)
    work = re.sub(r"\d+(?:\.\d+)?\s*股", " ", work)
    work = re.sub(r"(買了?|買入|加碼|每股|單價)", " ", work)
    work = re.sub(r"\s+", " ", work).strip()

    symbol_or_name = ""
    m_code = re.search(r"([A-Za-z]{0,2}\d{3,5}[A-Za-z]?)", work)
    if m_code:
        symbol_or_name = m_code.group(1)
        if re.search(r"[A-Za-z]", symbol_or_name):
            symbol_or_name = symbol_or_name.upper()
    else:
        m_name = re.search(r"([一-龥]{2,8}|[A-Za-z]{2,10})", work)
        if m_name:
            symbol_or_name = m_name.group(1)
    if not symbol_or_name:
        return None
    if symbol_or_name in {"今天", "昨天", "股票", "持股", "台股"}:
        return None
    return symbol_or_name, buy_price, qty


def add_holding(
    session: Session,
    user_id: str,
    symbol_or_name: str,
    buy_price: float,
    quantity: float = 1000,
    bought_at: Optional[datetime] = None,
    note: Optional[str] = None,
) -> str:
    data = record_holding(
        session,
        user_id,
        symbol_or_name=symbol_or_name,
        buy_price=buy_price,
        quantity=quantity,
        bought_at=bought_at,
        note=note,
    )
    if data.get("error"):
        return data["error"]
    return data["message"]


def record_holding(
    session: Session,
    user_id: str,
    symbol_or_name: str,
    buy_price: float,
    quantity: float = 1000,
    bought_at: Optional[datetime] = None,
    note: Optional[str] = None,
) -> dict:
    resolved, matched = resolve_symbol(symbol_or_name)
    if not resolved:
        return {"error": f"找不到「{symbol_or_name}」對應的台股代號，請改用代號（例如 2330）。"}
    if buy_price <= 0:
        return {"error": "買入價格必須大於 0。"}
    if quantity <= 0:
        return {"error": "股數必須大於 0。（1 張 = 1000 股）"}

    quote = get_stock_quote(resolved)
    name = quote.get("name") or matched or resolved
    item = Holding(
        user_id=user_id,
        seq=next_seq(session, Holding, user_id),
        symbol=resolved,
        name=name,
        buy_price=buy_price,
        quantity=quantity,
        bought_at=bought_at or datetime.now(),
        note=note,
        closed=0,
    )
    session.add(item)
    session.flush()
    cost = buy_price * quantity
    lots = quantity / 1000
    message = (
        f"已記錄持股 #{show_no(item)}：{resolved} {name}\n"
        f"買入 {buy_price} × {quantity:.0f} 股（約 {lots:g} 張）\n"
        f"成本約 ${cost:,.0f}"
    )
    return {
        "id": show_no(item),
        "symbol": resolved,
        "name": name,
        "buy_price": buy_price,
        "quantity": quantity,
        "lots": lots,
        "cost": cost,
        "message": message,
    }


def close_holding(session: Session, user_id: str, holding_id: int) -> str:
    item = find_by_seq(session, Holding, user_id, holding_id)
    if not item:
        return f"找不到持股 #{holding_id}。"
    if item.closed:
        return f"持股 #{show_no(item)} 已結清過了。"
    item.closed = 1
    return f"已結清持股 #{show_no(item)}：{item.symbol} {item.name or ''}".strip()


def portfolio_summary(session: Session, user_id: str) -> str:
    """計算目前持股的未實現盈虧與報酬率。"""
    data = portfolio_summary_data(session, user_id)
    if not data["rows"]:
        return "目前沒有持股紀錄。跟我說「我買了台積電 2400 元 1 張」就能開始記。"
    lines = ["📈 持股損益一覽"]
    for r in data["rows"]:
        if r.get("error"):
            lines.append(f"#{r['id']} {r['symbol']} {r.get('name') or ''}：目前報價取得失敗")
            continue
        arrow = "▲" if r["pnl"] > 0 else ("▼" if r["pnl"] < 0 else "－")
        lines.append(
            f"#{r['id']} {r['symbol']} {r.get('name') or ''}\n"
            f"　買入 {r['buy']} → 現價 {r['current']}  × {r['qty']}\n"
            f"　{arrow} 盈虧 ${r['pnl']:,.0f}（報酬率 {r['roi']:+.2f}%）"
        )
    total = data.get("total")
    if total:
        arrow = "▲" if total["pnl"] > 0 else ("▼" if total["pnl"] < 0 else "－")
        lines.append("—" * 8)
        lines.append(
            f"合計成本 ${total['cost']:,.0f}　市值 ${total['value']:,.0f}\n"
            f"{arrow} 總盈虧 ${total['pnl']:,.0f}（總報酬率 {total['roi']:+.2f}%）"
        )
    return "\n".join(lines)


def portfolio_summary_data(session: Session, user_id: str) -> dict:
    rows_db = (
        session.execute(
            select(Holding)
            .where(Holding.user_id == user_id, Holding.closed == 0)
            .order_by(Holding.id.asc())
        )
        .scalars()
        .all()
    )
    sold_map = _sold_quantity_map(session, [r.id for r in rows_db])
    rows: List[dict] = []
    total_cost = 0.0
    total_value = 0.0
    for r in rows_db:
        qty_left = _remaining_quantity(r, sold_map)
        if qty_left <= 0:
            continue
        q = get_stock_quote(r.symbol)
        if q.get("error") or q.get("price") is None:
            rows.append(
                {
                    "id": show_no(r),
                    "symbol": r.symbol,
                    "name": r.name or "",
                    "error": True,
                }
            )
            continue
        current = float(q["price"])
        cost = r.buy_price * qty_left
        value = current * qty_left
        pnl = value - cost
        roi = (pnl / cost * 100) if cost else 0.0
        total_cost += cost
        total_value += value
        rows.append(
            {
                "id": show_no(r),
                "symbol": r.symbol,
                "name": q.get("name") or r.name or "",
                "buy": r.buy_price,
                "current": current,
                "qty": f"{qty_left:.0f} 股",
                "pnl": pnl,
                "roi": roi,
            }
        )
    total = None
    if total_cost > 0:
        total_pnl = total_value - total_cost
        total = {
            "cost": total_cost,
            "value": total_value,
            "pnl": total_pnl,
            "roi": total_pnl / total_cost * 100,
        }
    return {"rows": rows, "total": total}


def _sold_quantity_map(session: Session, holding_ids: list[int]) -> dict[int, float]:
    if not holding_ids:
        return {}
    rows = session.execute(
        select(StockSaleLot.holding_id, func.sum(StockSaleLot.quantity))
        .where(StockSaleLot.holding_id.in_(holding_ids))
        .group_by(StockSaleLot.holding_id)
    ).all()
    return {int(hid): float(qty or 0) for hid, qty in rows}


def _remaining_quantity(holding: Holding, sold_map: dict[int, float]) -> float:
    if holding.closed and holding.id not in sold_map:
        return 0.0
    return max(0.0, float(holding.quantity) - sold_map.get(holding.id, 0.0))


def parse_stock_sell_utterance(
    text: str,
) -> Optional[tuple[str, float, Optional[float], Optional[float], bool, Optional[datetime]]]:
    """解析賣股句 → (代號或名稱, 賣價, 股數或 None, 淨損益或 None, 是否全數, 賣出時間)。

    例：賣出 00947 41元 全數／持有股 00947 41塊已全數賣出 淨賺12000
    """
    raw = (text or "").strip()
    if not raw or not re.search(r"(賣出|賣掉|售出|出清|全數賣|結清)", raw):
        return None
    if re.search(r"(早餐|午餐|晚餐|消夜|記帳|花費|消費|支出|行程|行事曆)", raw):
        return None

    pnl: Optional[float] = None
    m_pnl = re.search(
        r"(淨賺|賺了|淨賠|賠了|淨損益|損益|賺|賠)\s*[：:]?\s*([+-]?\d+(?:\.\d+)?)",
        raw,
    )
    if m_pnl:
        pnl = float(m_pnl.group(2))
        word = m_pnl.group(1)
        if word in {"淨賠", "賠了", "賠"}:
            pnl = -abs(pnl)
        elif word in {"淨賺", "賺了", "賺"}:
            pnl = abs(pnl)

    m_price = re.search(r"(?:@|每股|單價|賣在|賣價)?\s*(\d+(?:\.\d+)?)\s*(?:元|塊)", raw)
    if not m_price:
        m_price = re.search(r"@\s*(\d+(?:\.\d+)?)", raw)
    if not m_price:
        return None
    sell_price = float(m_price.group(1))

    sell_all = bool(re.search(r"(全數|全部|都賣|出清)", raw))
    qty: Optional[float] = None
    m_lot = re.search(r"(\d+(?:\.\d+)?)\s*張", raw)
    m_share = re.search(r"(\d+(?:\.\d+)?)\s*股", raw)
    if m_lot:
        qty = float(m_lot.group(1)) * 1000.0
        sell_all = False
    elif m_share:
        qty = float(m_share.group(1))
        sell_all = False

    work = raw
    if m_pnl:
        work = work[: m_pnl.start()] + " " + work[m_pnl.end() :]
    work = re.sub(r"(?:@|每股|單價|賣在|賣價)?\s*\d+(?:\.\d+)?\s*(?:元|塊)", " ", work)
    work = re.sub(r"@\s*\d+(?:\.\d+)?", " ", work)
    work = re.sub(r"\d+(?:\.\d+)?\s*張", " ", work)
    work = re.sub(r"\d+(?:\.\d+)?\s*股", " ", work)
    work = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}", " ", work)
    work = re.sub(
        r"(賣出|賣掉|售出|出清|全數賣出|全數|全部|都賣|結清|已|持有股|持有|股票|持股)",
        " ",
        work,
    )
    work = re.sub(r"\s+", " ", work).strip()

    symbol_or_name = ""
    m_code = re.search(r"([A-Za-z]{0,2}\d{3,6}[A-Za-z]?)", work)
    if m_code:
        symbol_or_name = m_code.group(1)
        if re.search(r"[A-Za-z]", symbol_or_name):
            symbol_or_name = symbol_or_name.upper()
    else:
        m_name = re.search(r"([一-龥]{2,8}|[A-Za-z]{2,10})", work)
        if m_name:
            symbol_or_name = m_name.group(1)
    if not symbol_or_name or symbol_or_name in {"今天", "昨天", "股票", "持股", "台股"}:
        return None

    sold_at = _sell_time(raw)
    return symbol_or_name, sell_price, qty, pnl, sell_all, sold_at


def _sell_time(text: str) -> datetime:
    raw = text or ""
    if re.search(r"前天", raw):
        return (datetime.now() - timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
    if re.search(r"昨天|昨日", raw):
        return (datetime.now() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2})", raw)
    if m:
        from .calendar_google import parse_user_date

        d = parse_user_date(m.group(1))
        if d:
            return d.replace(tzinfo=None, hour=12, minute=0, second=0, microsecond=0)
    return datetime.now().replace(microsecond=0)


def record_stock_sale(
    session: Session,
    user_id: str,
    text: str,
) -> dict:
    """賣出持有股。全數或指定股數，先進先出。有寫淨損益就用該數字。"""
    parsed = parse_stock_sell_utterance(text)
    if not parsed:
        return {"error": "請寫賣出哪一支、每股賣價，以及幾股或全數。例如：賣出 00947 41元 全數。"}
    symbol_or_name, sell_price, qty, user_pnl, sell_all, sold_at = parsed
    if sell_price <= 0:
        return {"error": "賣出價格必須大於 0。"}
    if not sell_all and (qty is None or qty <= 0):
        return {"error": "請寫賣出幾股、幾張，或說全數。例如：賣出 00947 41元 2張。"}

    resolved, matched = resolve_symbol(symbol_or_name)
    if not resolved:
        return {"error": f"找不到「{symbol_or_name}」對應的代號，請改用代號（例如 00947）。"}

    lots = (
        session.execute(
            select(Holding)
            .where(
                Holding.user_id == user_id,
                Holding.symbol == resolved,
                Holding.closed == 0,
            )
            .order_by(Holding.bought_at.asc(), Holding.id.asc())
        )
        .scalars()
        .all()
    )
    sold_map = _sold_quantity_map(session, [h.id for h in lots])
    open_lots = []
    for lot in lots:
        left = _remaining_quantity(lot, sold_map)
        if left > 0:
            open_lots.append((lot, left))
    if not open_lots:
        return {"error": f"目前沒有持有 {resolved}，無法賣出。"}

    total_left = sum(left for _, left in open_lots)
    if sell_all or qty is None:
        qty = total_left
    if qty > total_left + 1e-6:
        return {"error": f"{resolved} 目前還有 {total_left:.0f} 股，這次要賣 {qty:.0f} 股，超過了。"}

    name = matched or resolved
    try:
        quote = get_stock_quote(resolved)
        if quote.get("name"):
            name = quote["name"]
    except Exception:  # noqa: BLE001
        name = open_lots[0][0].name or name

    need = float(qty)
    calculated = 0.0
    allocs: list[tuple[Holding, float]] = []
    for lot, left in open_lots:
        if need <= 1e-9:
            break
        take = min(left, need)
        calculated += (sell_price - float(lot.buy_price)) * take
        allocs.append((lot, take))
        need -= take

    realized = float(user_pnl) if user_pnl is not None else calculated
    sale = StockSale(
        user_id=user_id,
        seq=next_seq(session, StockSale, user_id),
        symbol=resolved,
        name=name,
        sell_price=float(sell_price),
        quantity=float(qty),
        sold_at=sold_at,
        realized_pnl=realized,
        note=None if user_pnl is None else "自訂淨損益",
    )
    session.add(sale)
    session.flush()
    for lot, take in allocs:
        session.add(
            StockSaleLot(
                sale_id=sale.id,
                holding_id=lot.id,
                quantity=take,
                buy_price=float(lot.buy_price),
            )
        )
        left_after = _remaining_quantity(lot, sold_map) - take
        sold_map[lot.id] = sold_map.get(lot.id, 0.0) + take
        if left_after <= 1e-6:
            lot.closed = 1
    session.flush()

    lots_n = qty / 1000
    weekday = "一二三四五六日"[sold_at.weekday()]
    when_label = f"{sold_at.strftime('%Y/%m/%d')}（週{weekday}）"
    pnl_label = "你填的淨損益" if user_pnl is not None else "淨損益"
    message = (
        f"已賣出 #{show_no(sale)}：{resolved} {name}\n"
        f"賣價 {sell_price:g} × {qty:.0f} 股（約 {lots_n:g} 張）\n"
        f"{pnl_label} ${realized:,.0f}\n"
        f"日期：{when_label}"
    )
    return {
        "id": show_no(sale),
        "symbol": resolved,
        "name": name,
        "sell_price": float(sell_price),
        "quantity": float(qty),
        "lots": lots_n,
        "realized_pnl": realized,
        "when_label": when_label,
        "message": message,
    }


def query_stock_trades(session: Session, user_id: str, text: str) -> dict:
    """一段期間的買入與賣出，損益只算這段賣出的已實現金額。"""
    start, end, label, symbol = _trade_window(text)
    buys = (
        session.execute(
            select(Holding)
            .where(
                Holding.user_id == user_id,
                Holding.bought_at.is_not(None),
                Holding.bought_at >= start,
                Holding.bought_at < end,
            )
            .order_by(Holding.bought_at.asc(), Holding.id.asc())
        )
        .scalars()
        .all()
    )
    sales = (
        session.execute(
            select(StockSale)
            .where(
                StockSale.user_id == user_id,
                StockSale.sold_at.is_not(None),
                StockSale.sold_at >= start,
                StockSale.sold_at < end,
            )
            .order_by(StockSale.sold_at.asc(), StockSale.id.asc())
        )
        .scalars()
        .all()
    )
    if symbol:
        buys = [r for r in buys if r.symbol == symbol]
        sales = [r for r in sales if r.symbol == symbol]
        label = f"{label} {symbol}"

    events: list[tuple[datetime, str]] = []
    for r in buys:
        when = r.bought_at or r.created_at
        events.append(
            (
                when,
                f"買 #{show_no(r)} [{when.strftime('%m/%d')}] {r.symbol} {r.name or ''}  "
                f"{r.buy_price:g} × {r.quantity:.0f}股",
            )
        )
    total_pnl = 0.0
    for r in sales:
        when = r.sold_at or r.created_at
        total_pnl += float(r.realized_pnl or 0)
        events.append(
            (
                when,
                f"賣 #{show_no(r)} [{when.strftime('%m/%d')}] {r.symbol} {r.name or ''}  "
                f"{r.sell_price:g} × {r.quantity:.0f}股  淨損益 ${r.realized_pnl:,.0f}",
            )
        )
    events.sort(key=lambda x: (x[0], x[1]))
    lines = [line for _, line in events]
    if not lines:
        return {
            "empty": True,
            "label": label,
            "lines": [],
            "total_pnl": 0.0,
            "text": f"📒 {label}\n這段期間沒有買賣紀錄。",
        }
    text_out = "\n".join(
        [f"📒 {label} 買賣紀錄", *lines, "—" * 8, f"已實現淨損益 ${total_pnl:,.0f}"]
    )
    return {
        "empty": False,
        "label": label,
        "lines": lines,
        "total_pnl": total_pnl,
        "text": text_out,
    }


def _trade_symbol(text: str) -> Optional[str]:
    work = text or ""
    work = re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月", " ", work)
    work = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*[日號]", " ", work)
    work = re.sub(r"\d{1,2}\s*月", " ", work)
    work = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}", " ", work)
    work = re.sub(r"\d{4}\s*年", " ", work)
    m = re.search(r"(?<!\d)([A-Za-z]{0,2}\d{4,6}[A-Za-z]?)(?!\d)", work)
    if not m:
        return None
    token = m.group(1)
    if re.fullmatch(r"\d{4}", token) and token.startswith(("19", "20")):
        return None
    return token.upper() if re.search(r"[A-Za-z]", token) else token


def _trade_window(text: str) -> tuple[datetime, datetime, str, Optional[str]]:
    from .calendar_google import parse_user_month_range
    from .expenses import _resolve_period, extract_period_hint

    raw = (text or "").strip()
    symbol = _trade_symbol(raw)
    span = parse_user_month_range(raw)
    if span and re.search(r"(到|至|~|～|-|—|–)", raw):
        (y1, m1), (y2, m2) = span
        start = datetime(y1, m1, 1)
        end = datetime(y2 + 1, 1, 1) if m2 == 12 else datetime(y2, m2 + 1, 1)
        label = f"{y1}/{m1:02d}–{y2}/{m2:02d}"
        return start, end, label, symbol
    hint = extract_period_hint(raw, default="本月")
    start, end, label = _resolve_period(hint)
    return start, end, label, symbol


def list_holding_user_ids(session: Session) -> List[str]:
    rows = session.execute(
        select(Holding.user_id).where(Holding.closed == 0).distinct()
    ).all()
    return [r[0] for r in rows]
