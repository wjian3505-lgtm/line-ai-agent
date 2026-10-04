"""LINE 訊息路由：A 規則保底 + B Gemini 工具推理。

分工原則（加功能時先判 A/B）：
- A：意圖清楚、參數好抽、高頻／做錯代價高 → 不經 Gemini
- B：口語、多步驟、跨模組、A 沒命中 → Gemini + tools
- 回覆一律走 Flex Message（淺色簡約）；禁止 Markdown 星號
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import calendar_google
from . import flex_ui
from .config import settings
from .db import session_scope
from .expenses import (
    build_expense_full_report,
    delete_expense_record,
    record_income,
    record_refund,
    extract_period_hint,
    get_pending_images,
    parse_expense_utterances,
    public_media_url,
    record_expense,
    reset_pending_images,
)
from .intent import Intent, classify_intent, extract_tech_query, looks_like_expense_write
from .portfolio import (
    parse_stock_buy_utterance,
    portfolio_summary_data,
    query_stock_trades,
    record_holding,
    record_stock_sale,
    summarize_today_queries,
)
from .stock import (
    get_market_summary,
    get_stock_movers_rows,
    get_stock_quote,
)
from .tools import build_tools

logger = logging.getLogger(__name__)


@dataclass
class AgentReply:
    text: str
    image_urls: list[str] = field(default_factory=list)
    flex: Optional[dict[str, Any]] = None


def _reply(
    text: str,
    flex: Optional[dict] = None,
    images: Optional[list[str]] = None,
    *,
    title: str = "助理",
) -> AgentReply:
    clean = flex_ui.strip_markdown(text or "")
    card = flex or flex_ui.plain_card(title, clean or "（無內容）")
    return AgentReply(text=(clean or title)[:400], flex=card, image_urls=images or [])


# ---------------------------------------------------------------------------
# B：給 Gemini 的系統說明（工具細節以 tools docstring 為準）
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一位貼心、可靠的中文個人助理，服務於 LINE 聊天室。

你負責處理「規則路徑（A）沒接住」的口語需求，例如：
- 口語／不完整的記帳（規則抽不出金額或日期時）
- 新增行事曆、敘述句記買股、模糊問題、跨模組請求

規則：
- 一律繁體中文，回覆精簡、適合手機。
- 禁止使用 Markdown（不要用 **粗體**、* 星號清單、反引號）。用換行與「項目：內容」即可。
- 需要資料時必須呼叫工具，不要編造。
- 相對時間依「目前時間」換算成明確日期再存。
- 口語記帳請呼叫 add_expense（可留空 category 讓系統分類）；金額、項目、日期要盡量填對。
- 記帳 #編號 是記帳 id，不是行事曆或行程編號；刪記帳用 delete_expense。
- 查行事曆／行程一律用 Google：列表 google_calendar_list、新增 google_calendar_add、刪除 google_calendar_delete。
- 像「10/10 下午五點 演唱會」這種句子，請用 google_calendar_add（系統會自動備份本機）。
- 期間口語：1-10月、2-3月、8到10月、11-2月（跨年）、11月到明年2月、近三個月、上半年、第三季。
- 若使用者期間很模糊（例如寒假、開春到端午），先自行換算成明確起迄再呼叫 google_calendar_list；不要編造行程內容。
- 跨年預設：未寫年份且結束月 < 開始月（如 11-2月）→ 今年該月到明年該月。
- 查股價用 stock_quote；漲跌幅排行用 stock_movers；持股用 my_portfolio。
- 使用者說「判斷／分析」某檔股票、要買或賣的建議時，必須呼叫 stock_technical_analysis，不要只回 stock_quote。
- 若使用者問「這是最新股價嗎／準嗎／即時嗎」：說明持股卡的現價來自 Fugle 行情（開盤中為即時／近即時，收盤後為收盤價）；必要時可再呼叫 stock_quote 或 my_portfolio 核對，不要把整句話當成股票代號。
"""

_FALLBACK_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
)

_A_HINT = (
    "抱歉，AI 模型目前忙碌中（暫時塞車），請稍後再試。\n"
    "常用指令可不經 AI，直接傳：\n"
    "・今天早餐 100元／刪除#6（記帳）\n"
    "・今天花了多少／本月類別圓餅圖\n"
    "・這週行程／明天安排\n"
    "・2330、台積電股價、算我盈虧"
)


def _images_from_pending() -> list[str]:
    return [public_media_url(p) for p in get_pending_images()]


def _should_defer_expense_to_b(text: str) -> bool:
    if not looks_like_expense_write(text):
        return False
    return len(parse_expense_utterances(text)) == 0


# ===========================================================================
# A：規則路徑（穩定、高頻）— 由 intent.classify_intent 決定進哪一扇門
# ===========================================================================

def _expense_full_reply(user_id: str, period: str) -> AgentReply:
    """合計＋完整明細（carousel）＋圓餅圖。"""
    reset_pending_images()
    with session_scope() as session:
        report = build_expense_full_report(session, user_id, period)
    if report["empty"]:
        return _reply(report["text"], title="花費")
    flex = flex_ui.expense_full_report(
        label=report["label"],
        total=report["total"],
        count=report["count"],
        by_cat=report["by_cat"],
        detail_lines=report["detail_lines"],
        income=float(report.get("income") or 0),
        spend=float(report.get("spend") if report.get("spend") is not None else report["total"]),
    )
    income = float(report.get("income") or 0)
    headline = float(report.get("net") if income else report["total"])
    alt = f"{report['label']} 合計 ${headline:,.0f}（{report['count']} 筆）"
    return _reply(alt, flex, images=_images_from_pending(), title="花費報告")


def _a_expenses(user_id: str, text: str, intent: Intent) -> AgentReply | None:
    t = text.strip()

    if intent == Intent.EXPENSE_DELETE:
        m_del = re.search(
            r"(?:刪除|刪掉|刪去|移除|取消)\s*(?:這筆|該筆|記帳)?\s*[#＃]?\s*(\d{1,6})",
            t,
        )
        if not m_del:
            return None
        with session_scope() as session:
            msg = delete_expense_record(session, user_id, int(m_del.group(1)))
        return _reply(msg, title="記帳")

    if intent == Intent.EXPENSE_REFUND:
        with session_scope() as session:
            data = record_refund(session, user_id, t)
        if data.get("error"):
            return _reply(data["error"], title="收回")
        return _reply(
            data["message"],
            flex_ui.expense_added(
                expense_id=data["id"],
                amount=data["amount"],
                category=data["category"],
                note=data["note"],
                when_label=data["when_label"],
            ),
            title="收回",
        )

    if intent == Intent.EXPENSE_INCOME:
        with session_scope() as session:
            data = record_income(session, user_id, t)
        if data.get("error"):
            return _reply(data["error"], title="收入")
        return _reply(
            data["message"],
            flex_ui.expense_added(
                expense_id=data["id"],
                amount=data["amount"],
                category=data["category"],
                note=data["note"],
                when_label=data["when_label"],
            ),
            title="收入",
        )

    if intent in {Intent.EXPENSE_CHART, Intent.EXPENSE_QUERY}:
        period = extract_period_hint(
            t,
            default="今天" if re.search(r"今天|今日", t) else "本月",
        )
        return _expense_full_reply(user_id, period)

    if intent == Intent.EXPENSE_WRITE:
        items = parse_expense_utterances(t)
        if not items:
            return None
        saved: list[dict] = []
        with session_scope() as session:
            for amount, note, spent_at in items:
                data = record_expense(
                    session, user_id, amount=amount, note=note, spent_at=spent_at
                )
                if data.get("error"):
                    if saved:
                        break
                    return _reply(data["error"], title="記帳")
                saved.append(data)

        if not saved:
            return _reply("記帳失敗，請稍後再試。", title="記帳")

        if len(saved) == 1:
            data = saved[0]
            return _reply(
                data["message"],
                flex_ui.expense_added(
                    expense_id=data["id"],
                    amount=data["amount"],
                    category=data["category"],
                    note=data["note"],
                    when_label=data["when_label"],
                ),
                title="記帳",
            )

        total = sum(float(x["amount"]) for x in saved)
        lines = [
            f"已一次記帳 {len(saved)} 筆，合計 ${total:,.0f}",
            f"日期：{saved[0]['when_label']}",
        ]
        for x in saved:
            lines.append(f"#{x['id']} {x['note']} ${x['amount']:,.0f}（{x['category']}）")
        return _reply(
            "\n".join(lines),
            flex_ui.expenses_added_batch(
                rows=saved,
                when_label=saved[0]["when_label"],
            ),
            title="記帳",
        )

    return None


def _a_stocks(user_id: str, text: str, intent: Intent) -> AgentReply | None:
    t = text.strip()

    if intent == Intent.STOCK_TODAY_QUERIES:
        with session_scope() as session:
            msg = summarize_today_queries(session, user_id)
        return _reply(msg, title="今日查詢")

    if intent == Intent.STOCK_PORTFOLIO:
        with session_scope() as session:
            data = portfolio_summary_data(session, user_id)
        rows = [r for r in data["rows"] if not r.get("error")]
        alt = "持股損益" if rows or data["rows"] else "目前沒有持股紀錄"
        return _reply(
            alt,
            flex_ui.portfolio_card(rows=data["rows"], total=data.get("total")),
            title="持股損益",
        )

    if intent == Intent.STOCK_MARKET:
        index = get_stock_quote("IX0001")
        watch = [get_stock_quote(sym) for sym in settings.stock_watchlist]
        return _reply(
            get_market_summary(),
            flex_ui.market_summary_card(index=index, watch=watch),
            title="今日台股",
        )

    if intent == Intent.STOCK_MOVERS:
        if re.search(r"漲跌幅", t) or (
            re.search(r"漲幅|漲最多|台股\s*漲", t) and re.search(r"跌幅|跌最多|台股\s*跌", t)
        ):
            directions: tuple[str, ...] = ("up", "down")
        elif re.search(r"跌幅|跌最多|跌幅排行|(今日|今天)?\s*台股\s*跌", t):
            directions = ("down",)
        else:
            directions = ("up",)

        bubbles = []
        alts = []
        for direction in directions:
            title, date_label, rows = get_stock_movers_rows(direction, limit=10, market="ALL")
            if not rows:
                continue
            bubbles.append(
                flex_ui.stock_movers_card(
                    title=title, date_label=date_label, rows=rows, direction=direction
                )
            )
            alts.append(title)
        if not bubbles:
            return _reply("目前抓不到漲跌幅排行資料，請稍後再試。", title="台股排行")
        flex = bubbles[0] if len(bubbles) == 1 else flex_ui.carousel(bubbles)
        return _reply("／".join(alts), flex, title="台股排行")

    if intent != Intent.STOCK_QUOTE:
        return None

    m = re.fullmatch(
        r"(?:查詢?|看看?)?\s*"
        r"([A-Za-z]{0,2}\d{3,5}[A-Za-z]?)"
        r"\s*(?:的)?\s*(?:股價|報價|多少|現價)?",
        t,
    )
    if not m:
        m = re.fullmatch(
            r"(?:查詢?|看看?)?\s*"
            r"([一-龥A-Za-z]{1,8})"
            r"\s*(?:的)?\s*(?:股價|報價|多少|現價)",
            t,
        )
    if not m:
        m = re.fullmatch(r"([一-龥A-Za-z]{2,6})", t)
    if not m:
        return None
    query = m.group(1)

    from .portfolio import log_stock_query
    from .stock import format_quote

    with session_scope() as session:
        q = get_stock_quote(query)
        if q.get("error"):
            # 查不到且像口語／疑問 → 交給 Gemini，不要硬回「找不到代號」
            if re.search(r"[嗎嘛呢？?]|這是|最新|即時", t) or len(query) > 6:
                return None
            return _reply(format_quote(q), flex_ui.stock_quote_card(q), title="查詢失敗")
        log_stock_query(
            session,
            user_id,
            symbol=str(q.get("symbol") or query),
            name=q.get("name"),
            price=float(q["price"]) if q.get("price") is not None else None,
        )
        return _reply(format_quote(q), flex_ui.stock_quote_card(q), title="報價")


def _a_calendar(user_id: str, text: str, intent: Intent) -> AgentReply | None:
    t = text.strip()

    if intent == Intent.CALENDAR_DELETE:
        try:
            msg = calendar_google.delete_event_from_utterance(t)
            return _reply(msg, title="行事曆")
        except Exception as exc:  # noqa: BLE001
            return _reply(f"移除行程失敗：{exc}", title="行事曆")

    if intent == Intent.CALENDAR_ADD:
        try:
            with session_scope() as session:
                msg = calendar_google.add_event_from_utterance(
                    t,
                    local_session=session,
                    local_user_id=user_id,
                )
            # 規則抽不到參數時交 B
            if msg.startswith("無法理解"):
                return None
            return _reply(msg, title="行事曆")
        except Exception as exc:  # noqa: BLE001
            return _reply(f"新增行程失敗：{exc}", title="行事曆")

    if intent != Intent.CALENDAR_LIST:
        return None

    try:
        # 規則抽不出期間 → 交 B（模糊口語如「寒假那段」）
        if calendar_google.resolve_calendar_range(t) is None:
            logger.info("行程查詢交 B（規則抽不到期間）：%s", t[:60])
            return None
        label, rows = calendar_google.list_events_rows_for_period(t)
        if label.startswith("無法理解"):
            logger.info("行程查詢交 B（無法理解期間）：%s", t[:60])
            return None
        alt = f"{label} · {len(rows)} 筆" if rows else f"{label} · 無行程"
        return _reply(alt, flex_ui.calendar_events(label=label, rows=rows), title="行程")
    except Exception as exc:  # noqa: BLE001
        return _reply(f"查詢行事曆失敗：{exc}", title="行事曆")


def _a_stock_buy(user_id: str, text: str) -> AgentReply | None:
    """記錄持股：00981A 28塊 買2張／我買了欣興 844元 1張。"""
    parsed = parse_stock_buy_utterance(text.strip())
    if not parsed:
        return None
    symbol_or_name, buy_price, qty = parsed
    with session_scope() as session:
        data = record_holding(
            session,
            user_id,
            symbol_or_name=symbol_or_name,
            buy_price=buy_price,
            quantity=qty,
        )
    if data.get("error"):
        return _reply(data["error"], title="持股")
    return _reply(
        data["message"],
        flex_ui.holding_added(
            holding_id=data["id"],
            symbol=data["symbol"],
            name=data["name"],
            buy_price=data["buy_price"],
            quantity=data["quantity"],
            lots=data["lots"],
            cost=data["cost"],
        ),
        title="持股",
    )


def _a_stock_sell(user_id: str, text: str) -> AgentReply:
    with session_scope() as session:
        data = record_stock_sale(session, user_id, text.strip())
    if data.get("error"):
        return _reply(data["error"], title="賣出")
    return _reply(
        data["message"],
        flex_ui.holding_sold(
            sale_id=data["id"],
            symbol=data["symbol"],
            name=data["name"],
            sell_price=data["sell_price"],
            quantity=data["quantity"],
            lots=data["lots"],
            realized_pnl=data["realized_pnl"],
            when_label=data["when_label"],
        ),
        title="賣出",
    )


def _a_stock_trades(user_id: str, text: str) -> AgentReply:
    with session_scope() as session:
        report = query_stock_trades(session, user_id, text.strip())
    if report["empty"]:
        return _reply(report["text"], title="買賣紀錄")
    return _reply(
        report["text"],
        flex_ui.stock_trades_card(
            label=report["label"],
            lines=report["lines"],
            total_pnl=report["total_pnl"],
        ),
        title="買賣紀錄",
    )


def _a_stock_tech(text: str) -> AgentReply:
    """技術面判定：判斷／分析 + 代碼或名稱。"""
    from .analyzer import DISCLAIMER, run_analysis

    query = extract_tech_query(text)
    if not query:
        return _reply(
            "請加上股票代碼或名稱，例如：判斷 2330、分析台積電、分析 AAPL。",
            title="技術面判定",
        )
    result = run_analysis(query)
    if not result.get("ok"):
        return _reply(result.get("error") or "技術面判定失敗。", title="技術面判定")
    payload = result["payload"]
    alt = result.get("alt_text") or f"{result.get('symbol')} 技術面判定"
    return _reply(
        alt,
        flex_ui.stock_tech_card(
            symbol=str(result.get("symbol") or query),
            name=str(result.get("name") or ""),
            verdict=str(payload.get("verdict") or "觀望"),
            action=str(payload.get("action") or ""),
            matched_rules=list(payload.get("matched_rules") or []),
            failed_rules=list(payload.get("failed_rules") or []),
            risk_note=str(payload.get("risk_note") or "—"),
            confidence=str(payload.get("confidence") or "中"),
            snapshot=payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else None,
            disclaimer=DISCLAIMER,
        ),
        title="技術面判定",
    )


def try_route_a(user_id: str, text: str) -> AgentReply | None:
    """先 classify_intent，再進對應 handler（不再靠各 handler 互搶正則）。"""
    t = (text or "").strip()
    if not t:
        return None

    intent = classify_intent(t)
    logger.info("A 意圖：%s ← %s", intent.value, t[:80])

    if intent == Intent.STOCK_BUY:
        return _a_stock_buy(user_id, t)

    if intent == Intent.STOCK_SELL:
        return _a_stock_sell(user_id, t)

    if intent == Intent.STOCK_TRADES:
        return _a_stock_trades(user_id, t)

    if intent == Intent.STOCK_TECH:
        return _a_stock_tech(t)

    if intent in {
        Intent.EXPENSE_DELETE,
        Intent.EXPENSE_CHART,
        Intent.EXPENSE_QUERY,
        Intent.EXPENSE_WRITE,
        Intent.EXPENSE_REFUND,
        Intent.EXPENSE_INCOME,
    }:
        reply = _a_expenses(user_id, t, intent)
        if reply is not None:
            return reply
        if intent == Intent.EXPENSE_WRITE and _should_defer_expense_to_b(t):
            logger.info("記帳交 B（規則抽不到）：%s", t[:60])
            return None
        return reply

    if intent in {Intent.CALENDAR_DELETE, Intent.CALENDAR_LIST, Intent.CALENDAR_ADD}:
        return _a_calendar(user_id, t, intent)

    if intent in {
        Intent.STOCK_TODAY_QUERIES,
        Intent.STOCK_PORTFOLIO,
        Intent.STOCK_MARKET,
        Intent.STOCK_MOVERS,
        Intent.STOCK_QUOTE,
    }:
        return _a_stocks(user_id, t, intent)

    return None


# ===========================================================================
# B：Gemini + tools
# ===========================================================================

def _build_client():
    from google import genai

    return genai.Client(api_key=settings.gemini_api_key)


def _is_transient_gemini_error(exc: Exception) -> bool:
    text = str(exc)
    return any(
        key in text
        for key in (
            "503",
            "UNAVAILABLE",
            "429",
            "RESOURCE_EXHAUSTED",
            "high demand",
            "try again later",
        )
    )


def _should_use_portfolio_flex(user_text: str, reply: str = "") -> bool:
    """持股／收益相關句 → 一律用有紅綠的持股 Flex。"""
    t = f"{user_text or ''}\n{reply or ''}"
    if re.search(r"(花費|記帳|支出|消費)", user_text or "") and not re.search(
        r"(股|持股|股票|投資)", user_text or ""
    ):
        return False
    return bool(
        re.search(
            r"(持股|持倉|投資組合|盈虧|報酬|收益|獲利|損益|買入價|現在價|總市值|總成本|賺\s*\d|賠\s*\d|"
            r"我的股票|股票淨利|淨值)",
            t,
        )
    )


def _portfolio_flex_reply(user_id: str) -> AgentReply:
    with session_scope() as session:
        data = portfolio_summary_data(session, user_id)
    return _reply(
        "持股收益",
        flex_ui.portfolio_card(rows=data["rows"], total=data.get("total")),
        title="持股收益",
    )


def _candidate_models() -> list[str]:
    models: list[str] = []
    for m in (settings.gemini_model, *_FALLBACK_MODELS):
        if m and m not in models:
            models.append(m)
    return models


def ask_gemini_b(user_id: str, text: str) -> AgentReply:
    from google.genai import types

    # 收益／持股類不交給 Gemini 長文（無法上色），直接出 Flex
    if _should_use_portfolio_flex(text):
        return _portfolio_flex_reply(user_id)

    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    system_instruction = f"{SYSTEM_PROMPT}\n\n目前時間：{now}（時區 {settings.timezone}）"
    last_err: Exception | None = None

    reset_pending_images()
    with session_scope() as session:
        tools = build_tools(session, user_id)
        client = _build_client()
        for model in _candidate_models():
            for attempt in range(2):
                try:
                    chat = client.chats.create(
                        model=model,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            tools=tools,
                        ),
                    )
                    response = chat.send_message(text)
                    reply = (response.text or "").strip()
                    if model != settings.gemini_model:
                        logger.info("改用備援模型成功：%s", model)
                    if _should_use_portfolio_flex(text, reply):
                        return _portfolio_flex_reply(user_id)
                    # 「判斷／分析」絕不能被包成股價卡
                    if re.search(r"(判斷|分析|技術面)", text or ""):
                        return _reply(reply or "技術面判定失敗。", title="技術面判定")
                    # 若回文像股價漲跌，仍盡量紅漲綠跌上色
                    if re.search(r"(\(\s*[+\-]|\+\d|\-\d|▲|▼|漲|跌|賺|賠)", reply or ""):
                        return AgentReply(
                            text=(flex_ui.strip_markdown(reply) or "股價")[:400],
                            flex=flex_ui.stock_pnl_text_card("股價／收益", reply),
                            image_urls=_images_from_pending(),
                        )
                    return _reply(
                        reply or "我收到了，但暫時沒有可回覆的內容。",
                        images=_images_from_pending(),
                        title="助理",
                    )
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if _is_transient_gemini_error(exc) and attempt == 0:
                        logger.warning("模型 %s 暫時不可用，重試中：%s", model, exc)
                        time.sleep(1.2)
                        continue
                    if _is_transient_gemini_error(exc):
                        logger.warning("模型 %s 仍失敗，改試下一個：%s", model, exc)
                        break
                    raise

    raise last_err or RuntimeError("Gemini 無法回應")


def handle_user_message(user_id: str, text: str) -> AgentReply:
    """A 先；沒命中再 B。回覆帶 Flex。"""
    # 硬閘：判斷／分析某檔 → 技術面買賣建議，不要落到查股價
    if extract_tech_query(text or ""):
        return _a_stock_tech(text)

    # 硬閘：收益／持股句直接出紅漲綠跌卡，避免被 Gemini 黑字蓋掉
    if _should_use_portfolio_flex(text or ""):
        return _portfolio_flex_reply(user_id)

    a = try_route_a(user_id, text)
    if a is not None:
        return a

    if not settings.gemini_api_key:
        return _reply("尚未設定 GEMINI_API_KEY，無法啟用 AI 回覆。")

    try:
        return ask_gemini_b(user_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("B 路徑（Gemini）失敗")
        if _is_transient_gemini_error(exc):
            return _reply(_A_HINT, title="系統")
        return _reply(f"抱歉，處理時發生錯誤：{exc}", title="系統")


_try_fast_path = try_route_a
_ask_gemini = ask_gemini_b
