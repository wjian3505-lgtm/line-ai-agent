"""持股盈虧與查詢紀錄整理。"""
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Holding, StockQueryLog
from .stock import get_stock_quote, resolve_symbol


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
        f"已記錄持股 #{item.id}：{resolved} {name}\n"
        f"買入 {buy_price} × {quantity:.0f} 股（約 {lots:g} 張）\n"
        f"成本約 ${cost:,.0f}"
    )
    return {
        "id": item.id,
        "symbol": resolved,
        "name": name,
        "buy_price": buy_price,
        "quantity": quantity,
        "lots": lots,
        "cost": cost,
        "message": message,
    }


def close_holding(session: Session, user_id: str, holding_id: int) -> str:
    item = session.get(Holding, holding_id)
    if not item or item.user_id != user_id:
        return f"找不到持股 #{holding_id}。"
    if item.closed:
        return f"持股 #{holding_id} 已結清過了。"
    item.closed = 1
    return f"已結清持股 #{holding_id}：{item.symbol} {item.name or ''}".strip()


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
    rows: List[dict] = []
    total_cost = 0.0
    total_value = 0.0
    for r in rows_db:
        q = get_stock_quote(r.symbol)
        if q.get("error") or q.get("price") is None:
            rows.append(
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "name": r.name or "",
                    "error": True,
                }
            )
            continue
        current = float(q["price"])
        cost = r.buy_price * r.quantity
        value = current * r.quantity
        pnl = value - cost
        roi = (pnl / cost * 100) if cost else 0.0
        total_cost += cost
        total_value += value
        rows.append(
            {
                "id": r.id,
                "symbol": r.symbol,
                "name": q.get("name") or r.name or "",
                "buy": r.buy_price,
                "current": current,
                "qty": f"{r.quantity:.0f} 股",
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


def list_holding_user_ids(session: Session) -> List[str]:
    rows = session.execute(
        select(Holding.user_id).where(Holding.closed == 0).distinct()
    ).all()
    return [r[0] for r in rows]
