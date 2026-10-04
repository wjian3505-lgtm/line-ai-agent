"""AI Agent 可呼叫的工具函式。

以工廠函式 build_tools() 產生「綁定特定使用者與資料庫 Session」的閉包，
再交給 Gemini 的自動 function calling 使用。每個函式都有型別註解與
docstring，Gemini SDK 會據此自動產生工具 schema。

注意：此檔案不要加 `from __future__ import annotations`。
google-genai 的自動 function calling 會用 isinstance 檢查參數型別，
若註解被轉成字串會觸發 isinstance() 錯誤。
"""
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Note, Schedule
from .expenses import (
    add_expense_record,
    delete_expense_record,
)
from .portfolio import (
    add_holding,
    close_holding,
    log_stock_query,
    portfolio_summary,
    summarize_today_queries,
)
from . import calendar_google
from .stock import format_quote, get_market_summary, get_stock_movers, get_stock_movers_both, get_stock_quote

logger = logging.getLogger(__name__)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """盡量寬鬆地解析日期時間字串。"""
    if not value:
        return None
    value = value.strip()
    fmts = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_tools(session: Session, user_id: str) -> list[Callable]:
    """回傳綁定此使用者的工具函式清單。"""

    # ---------- 行程 ----------
    def add_schedule(title: str, start_at: str = "", location: str = "", note: str = "") -> str:
        """新增一筆行程或待辦事項。

        Args:
            title: 行程標題，例如「和客戶開會」。
            start_at: 開始時間，格式如 2026-07-30 14:00；沒有明確時間可留空。
            location: 地點（可選）。
            note: 備註（可選）。
        """
        item = Schedule(
            user_id=user_id,
            title=title,
            start_at=_parse_dt(start_at),
            location=location or None,
            note=note or None,
        )
        session.add(item)
        session.flush()
        when = item.start_at.strftime("%Y-%m-%d %H:%M") if item.start_at else "未指定時間"
        return f"已新增行程 #{item.id}：{title}（{when}）"

    def list_schedules(keyword: str = "", days_ahead: int = 30, include_done: bool = False) -> str:
        """查詢行程。可用關鍵字或未來天數範圍過濾。

        Args:
            keyword: 標題關鍵字（可選）。
            days_ahead: 查詢從今天起未來幾天內的行程，預設 30 天。
            include_done: 是否包含已完成的行程。
        """
        stmt = select(Schedule).where(Schedule.user_id == user_id)
        if not include_done:
            stmt = stmt.where(Schedule.done == 0)
        if keyword:
            stmt = stmt.where(Schedule.title.contains(keyword))
        stmt = stmt.order_by(Schedule.start_at.is_(None), Schedule.start_at.asc())
        rows = session.execute(stmt).scalars().all()

        if days_ahead and days_ahead > 0:
            limit = datetime.now() + timedelta(days=days_ahead)
            rows = [r for r in rows if r.start_at is None or r.start_at <= limit]

        if not rows:
            return "目前沒有符合條件的行程。"
        lines = ["你的行程："]
        for r in rows:
            when = r.start_at.strftime("%m/%d %H:%M") if r.start_at else "未定時間"
            loc = f" @ {r.location}" if r.location else ""
            lines.append(f"#{r.id} [{when}] {r.title}{loc}")
        return "\n".join(lines)

    def complete_schedule(schedule_id: int) -> str:
        """把指定編號的行程標記為已完成。

        Args:
            schedule_id: 行程編號（list_schedules 顯示的 #id）。
        """
        item = session.get(Schedule, schedule_id)
        if not item or item.user_id != user_id:
            return f"找不到編號 #{schedule_id} 的行程。"
        item.done = 1
        return f"已完成行程 #{schedule_id}：{item.title}"

    # ---------- 記帳 ----------
    def add_expense(amount: float, note: str = "", category: str = "", spent_at: str = "") -> str:
        """記錄一筆花費。若未指定 category，會依項目內容自動判斷類別。

        類別只能是：伙食、購物、交通、娛樂、投資、其他。

        Args:
            amount: 金額（新台幣）。
            note: 消費項目，例如 早餐、捷運、電影。
            category: 可選；不填會自動分類。
            spent_at: 消費日期，格式 2026-08-06；留空預設現在。
        """
        return add_expense_record(
            session,
            user_id,
            amount=amount,
            note=note,
            category=category,
            spent_at=_parse_dt(spent_at),
        )

    def list_expenses(date: str) -> str:
        """查詢某一天／某月的記帳細項與合計（含類別；系統會附圓餅圖）。

        Args:
            date: 例如 今天、昨天、8/6、八月、2026-08-06。
        """
        from .expenses import build_expense_full_report

        return build_expense_full_report(session, user_id, date)["text"]

    def summarize_expenses(period: str = "month") -> str:
        """統計某段期間總花費、類別占比與完整細項（系統會附圓餅圖）。

        Args:
            period: 今天 / 本月 / 今年 / 總共 / 八月 / 2026。
        """
        from .expenses import build_expense_full_report

        return build_expense_full_report(session, user_id, period)["text"]

    def expense_category_chart(period: str = "month") -> str:
        """產生消費類別圓餅圖＋合計與完整細項。

        當使用者說「消費類別圓餅圖」「類別統計圖」「八月花費」時使用。

        Args:
            period: 今天 / 本月 / 今年 / 總共 / 八月。
        """
        from .expenses import build_expense_full_report

        report = build_expense_full_report(session, user_id, period)
        if report.get("chart_path"):
            return report["text"] + "\n\n（已附上圓餅圖）"
        return report["text"]

    def delete_expense(expense_id: int) -> str:
        """刪除一筆記帳紀錄。

        當使用者說「刪除#6」「刪掉這筆記帳 #6」「取消第 6 筆」時使用。
        編號來自 list_expenses / summarize_expenses 顯示的 #id。
        注意：這是記帳編號，不是 Google 行事曆或行程編號。

        Args:
            expense_id: 記帳編號，例如 6。
        """
        return delete_expense_record(session, user_id, expense_id)

    # ---------- 筆記 ----------
    def add_note(content: str) -> str:
        """記下一則備忘 / 筆記。

        Args:
            content: 要記錄的內容。
        """
        item = Note(user_id=user_id, content=content)
        session.add(item)
        session.flush()
        return f"已記下筆記 #{item.id}。"

    def search_notes(keyword: str = "", limit: int = 10) -> str:
        """搜尋 / 列出筆記。

        Args:
            keyword: 關鍵字（可選）。
            limit: 最多回傳幾筆，預設 10。
        """
        stmt = select(Note).where(Note.user_id == user_id)
        if keyword:
            stmt = stmt.where(Note.content.contains(keyword))
        stmt = stmt.order_by(Note.created_at.desc()).limit(limit)
        rows = session.execute(stmt).scalars().all()
        if not rows:
            return "沒有找到相關筆記。"
        lines = ["你的筆記："]
        for r in rows:
            ts = r.created_at.strftime("%m/%d") if r.created_at else ""
            lines.append(f"#{r.id} [{ts}] {r.content}")
        return "\n".join(lines)

    # ---------- 股市 ----------
    def stock_quote(symbol: str) -> str:
        """查詢「單一個股」的即時報價與漲跌。只要現價／漲跌時用這個。

        若使用者要「判斷／分析／該買還是賣／技術面」，請改用 stock_technical_analysis，不要只用本工具。

        Args:
            symbol: 台股代號或公司名稱，例如 2330、台積電、3037、欣興、0050、00947。
        """
        q = get_stock_quote(symbol)
        if not q.get("error"):
            log_stock_query(
                session,
                user_id,
                symbol=str(q.get("symbol") or symbol),
                name=q.get("name"),
                price=float(q["price"]) if q.get("price") is not None else None,
            )
        return format_quote(q)

    def market_summary() -> str:
        """整理「今日台股大盤整體」收盤概況（加權指數＋預設自選股）。

        僅用於使用者想看大盤 / 整體 / 今日收盤總覽時。
        查詢某一支特定股票時請改用 stock_quote，不要用這個工具。
        """
        return get_market_summary()

    def stock_movers(direction: str = "both", limit: int = 10, market: str = "ALL") -> str:
        """查詢台股個股漲幅 / 跌幅排行榜（由多到少前 N 名）。

        當使用者問「漲幅前十」「跌最多的股票」「漲跌幅排行」時使用。

        Args:
            direction: up=只列漲幅、down=只列跌幅、both=兩邊都列（預設）。
            limit: 筆數，預設 10，最多 30。
            market: ALL=上市+上櫃、TSE=上市、OTC=上櫃。
        """
        d = (direction or "both").lower().strip()
        if d in ("both", "全部", "漲跌", "漲跌幅"):
            return get_stock_movers_both(limit=limit, market=market)
        return get_stock_movers(direction=d, limit=limit, market=market)

    def stock_technical_analysis(symbol: str) -> str:
        """依布林、RSI、MACD 規則給出買入／賣出／觀望建議。

        當使用者說「判斷 2330」「分析台積電」「00947 該買還是賣」「技術面」時必須用這個。
        不要用 stock_quote 代替。

        Args:
            symbol: 台股或美股代號、或公司名稱，例如 2330、00947、台積電、AAPL。
        """
        from .analyzer import run_analysis

        result = run_analysis(symbol)
        if not result.get("ok"):
            return result.get("error") or "技術面判定失敗。"
        return result.get("alt_text") or str(result.get("payload") or "")

    def today_stock_queries() -> str:
        """統整使用者「今天」查詢過哪些股票（含當時價格）。

        當使用者問「我今天查了哪些股票」「今天看了什麼股」時使用。
        """
        return summarize_today_queries(session, user_id)

    def record_stock_buy(
        symbol: str,
        buy_price: float,
        quantity: float = 1000,
        bought_at: str = "",
        note: str = "",
    ) -> str:
        """記錄一筆買入持股，之後可計算盈虧與報酬率。

        Args:
            symbol: 台股代號或公司名稱，例如 2330 或 台積電。
            buy_price: 買入每股價格（新台幣）。
            quantity: 股數。預設 1000（=1 張）。若使用者說「2 張」請傳 2000。
            bought_at: 買入日期，格式 2026-08-05；可留空。
            note: 備註（可選）。
        """
        return add_holding(
            session,
            user_id,
            symbol_or_name=symbol,
            buy_price=buy_price,
            quantity=quantity,
            bought_at=_parse_dt(bought_at),
            note=note or None,
        )

    def record_stock_sell(text: str) -> str:
        """賣出目前持有的股票。

        使用者說「賣出 00947 41元 全數」「賣掉 2 張」「淨賺 12000」時使用。
        不要把這種句子記成日常花費。

        Args:
            text: 使用者原句，需含股票、賣價，以及股數或全數。
        """
        from .portfolio import record_stock_sale

        data = record_stock_sale(session, user_id, text)
        if data.get("error"):
            return data["error"]
        return data["message"]

    def list_stock_trades(period: str) -> str:
        """查某月或某段月份的股票買賣紀錄，以及這段賣出的已實現淨損益。

        例如「10月買賣」「8月到10月股票損益」「10月 00947 買賣」。
        不要拿來查日常消費。

        Args:
            period: 使用者原句或期間。
        """
        from .portfolio import query_stock_trades

        return query_stock_trades(session, user_id, period)["text"]

    def my_portfolio() -> str:
        """查看目前持股清單，並用即時股價計算每檔與合計的盈虧、報酬率。

        當使用者問「我的持股」「算我盈虧」「報酬率多少」時使用。
        """
        return portfolio_summary(session, user_id)

    def close_stock_holding(holding_id: int) -> str:
        """把指定編號的持股標記為已結清（賣出後不再列入損益）。

        Args:
            holding_id: 持股編號（my_portfolio 顯示的 #id）。
        """
        return close_holding(session, user_id, holding_id)

    # ---------- Google 行事曆 ----------
    def google_calendar_list(period: str) -> str:
        """查詢 Google 行事曆在某段期間的行程。

        當使用者問以下類型句子時使用：
        - 今天 / 明天 / 後天有什麼行程
        - 我這週有什麼行程、下禮拜安排、週末有事嗎
        - 這週三有什麼安排、8/27 行程
        - 八月有哪些行程、本月行程、下個月安排
        - 8-10月、1-10月、2-3月、11-2月（跨年）、八月到十月
        - 11月到明年2月、2025年11月到2026年2月
        - 8/1到10/31、近三個月、未來兩個月
        - 上半年、下半年、第三季、Q3
        - 今年行程、最近有什麼行程、查行事曆
        - 模糊口語（寒假、開春那段）也可傳原句，工具會盡力解析

        Args:
            period: 使用者原句或期間，例如 1-10月、2-3月、11-2月、這週、今年。
        """
        return calendar_google.list_events_for_period(period)

    def google_calendar_add(
        title: str,
        start_at: str,
        end_at: str = "",
        location: str = "",
        description: str = "",
    ) -> str:
        """新增一筆行程到 Google 行事曆（成功後會自動備份到本機 schedules）。

        當使用者說「幫我加行程到行事曆」「8/27 晚上七點去大巨蛋看演唱會」時使用。

        Args:
            title: 行程標題，例如「大巨蛋演唱會」。
            start_at: 開始時間，例如 2026-08-27 19:00 或 8/27 晚上七點。
            end_at: 結束時間（可選）；沒給預設開始後 2 小時。
            location: 地點（可選），例如 大巨蛋。
            description: 備註（可選）。
        """
        return calendar_google.add_event(
            title=title,
            start_at=start_at,
            end_at=end_at,
            location=location,
            description=description,
            local_session=session,
            local_user_id=user_id,
        )

    def google_calendar_delete(
        title: str = "",
        when: str = "",
        hour: int = -1,
    ) -> str:
        """從 Google 行事曆移除行程。

        當使用者說「移除明天七點開會」「刪除行程」「取消後天會議」時使用。
        若找到多筆會列出請使用者講清楚，不會亂刪。

        Args:
            title: 標題關鍵字，例如 開會、演唱會。
            when: 日期或期間，例如 明天、8/27、這週。
            hour: 開始小時（0–23）；不確定可填 -1。
        """
        h = None if hour is None or hour < 0 else int(hour)
        return calendar_google.delete_event(title=title, when=when or title, hour=h)

    return [
        add_expense,
        list_expenses,
        summarize_expenses,
        expense_category_chart,
        delete_expense,
        add_note,
        search_notes,
        stock_quote,
        stock_technical_analysis,
        market_summary,
        stock_movers,
        today_stock_queries,
        record_stock_buy,
        record_stock_sell,
        list_stock_trades,
        my_portfolio,
        close_stock_holding,
        google_calendar_list,
        google_calendar_add,
        google_calendar_delete,
    ]
