"""統一意圖分流：先判領域，再進對應 handler。

一次解決「記帳／股價／行程互搶」的低級誤判：
- 各領域有明確關鍵字與互斥規則
- 股價短句極嚴格：純代號，或「名稱+股價」；含記帳詞一律不走股價
- 新功能請在此註冊 Intent，不要只在單一 handler 加正則
"""
from __future__ import annotations

import re
from enum import Enum

from .expenses import looks_like_income, looks_like_refund


class Intent(str, Enum):
    STOCK_BUY = "stock_buy"
    STOCK_SELL = "stock_sell"
    STOCK_TRADES = "stock_trades"
    EXPENSE_DELETE = "expense_delete"
    EXPENSE_CHART = "expense_chart"
    EXPENSE_QUERY = "expense_query"
    EXPENSE_WRITE = "expense_write"
    EXPENSE_REFUND = "expense_refund"
    EXPENSE_INCOME = "expense_income"
    CALENDAR_DELETE = "calendar_delete"
    CALENDAR_ADD = "calendar_add"  # 交 B
    CALENDAR_LIST = "calendar_list"
    STOCK_MOVERS = "stock_movers"
    STOCK_PORTFOLIO = "stock_portfolio"
    STOCK_TODAY_QUERIES = "stock_today_queries"
    STOCK_MARKET = "stock_market"
    STOCK_QUOTE = "stock_quote"
    STOCK_TECH = "stock_tech"
    OTHER = "other"  # 交 B


# 記帳領域詞：出現時禁止當股價名稱
_EXPENSE_DOMAIN = re.compile(
    r"(記帳|花費|支出|消費|花了|花多少|總花費|總支出|總消費|"
    r"明細|細項|圓餅|類別統計|早餐|午餐|晚餐|消夜|便當|捷運|咖啡)"
)

_CALENDAR_DOMAIN = re.compile(r"(行程|安排|行事曆|日程|行程表|開會|會議)")

_STOCK_BLOCK_AS_NAME = re.compile(
    r"(今天|今日|明天|昨天|你好|謝謝|大盤|八月|本月|今年|"
    r"花費|記帳|行程|安排|消費|支出|明細|盈虧|報酬|"
    r"這是|那是|是不是|對不對|最新|即時|即時嗎|真的|準不準|"
    r"可以|能不能|為什麼|怎麼|如何|"
    r"我的|股票|淨利|淨值|資產|持有|目前|全部|清單|列表|"
    r"判斷|分析|技術面)"
)

# 口語疑問句：應交給 Gemini，不要當公司名／股價查詢
_STOCK_META_QUESTION = re.compile(
    r"(這是|那是|是不是|對不對).{0,12}(股價|報價|現價|最新|即時)|"
    r"(股價|報價|現價).{0,6}(嗎|嘛|？|\?)|"
    r"^(最新|即時)?\s*(股價|報價)\s*(嗎|嘛|？|\?)?$"
)


def _has_stock_code(t: str) -> bool:
    return bool(re.search(r"[A-Za-z]{0,2}\d{3,5}[A-Za-z]?", t))


def looks_like_stock_buy(text: str) -> bool:
    t = (text or "").strip()
    if not t or not re.search(r"買(?!賣)", t):
        return False
    if re.search(r"(早餐|午餐|晚餐|消夜|捷運|記帳|花了多少|行程)", t):
        return False
    return bool(
        re.search(r"張|股", t)
        or _has_stock_code(t)
        or re.search(r"(買了?|買入|加碼)\s*[一-龥A-Za-z]{2,8}", t)
    )


def looks_like_stock_sell(text: str) -> bool:
    """賣出持股。須在記帳之前判斷，避免「41塊」被記成花費。"""
    t = (text or "").strip()
    if not t or not re.search(r"(賣出|賣掉|售出|出清|全數賣|結清)", t):
        return False
    if re.search(r"(早餐|午餐|晚餐|消夜|記帳|花費|消費|支出|行程|行事曆|判斷|分析|技術面)", t):
        return False
    return bool(
        re.search(r"[A-Za-z]{0,2}\d{3,6}[A-Za-z]?", t)
        or re.search(r"(持有|持股|股票|張)", t)
        or re.search(r"(賣出|賣掉|出清)\s*[一-龥A-Za-z]{2,8}", t)
    )


def looks_like_stock_trades(text: str) -> bool:
    """查一段期間的買賣紀錄與已實現損益。"""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"(行程|安排|行事曆|日程|消費|花費|支出|記帳|早餐|午餐|晚餐)", t):
        return False
    if re.search(r"(買賣紀錄|交易紀錄|成交紀錄|賣出紀錄|買入紀錄|股票買賣|股票損益|已實現)", t):
        return True
    if re.search(r"買賣", t) and re.search(r"(月|今年|去年|全部|至今)", t):
        return True
    if re.search(r"(股票|持股).{0,6}(損益|買賣|交易)|(損益|買賣|交易).{0,6}(股票|持股)", t):
        return True
    return False


def looks_like_calendar_add(text: str) -> bool:
    """像「10/10 下午五點 優里演唱會」或「幫我加行程…」的新增句。"""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"(記帳|花了|花費|支出|股價|盈虧|報酬|買了?\s*\d|元|塊)", t):
        return False
    if re.search(r"(行程|行事曆|安排)", t) and re.search(
        r"(加|新增|建立|幫我加|記到|寫進|幫我排)", t
    ):
        return True
    has_date = bool(
        re.search(
            r"\d{1,2}[/-]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}|明天|後天|大後天|今天|今日",
            t,
        )
    )
    has_time = bool(
        re.search(
            r"\d{1,2}:\d{2}|(早上|上午|中午|下午|晚上|傍晚)?\s*[一二三四五六七八九十兩\d]{1,3}\s*點",
            t,
        )
    )
    # 日期+時間+標題，避免只有「10/10」被誤判
    return bool(has_date and has_time and len(t) >= 8)


def looks_like_expense_period_query(text: str) -> bool:
    """月份／日期查帳，不是記一筆。

    「10月」「10月消費」「上個月花費」「4號」這類句子裡的數字是時間。
    「10月晚餐 300」「今天早餐 100元」仍有另一個金額，不當查詢。
    """
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"(行程|安排|行事曆|日程|有什麼|有哪些|有事嗎|股價|漲幅|跌幅|持股)", t):
        return False
    has_period = bool(
        re.search(
            r"(\d{1,2}\s*月(份)?|"
            r"(十[一二]|[一二三四五六七八九十兩])\s*月(份)?|"
            r"本月|這個月|當月|上個月|上月|下個月|下月|"
            r"今天|今日|昨天|昨日|前天|"
            r"今年|去年|明年|本年|"
            r"\d{4}\s*年|"
            r"\d{1,2}[/-]\d{1,2}|"
            r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
            r"\d{1,2}\s*[號日]|"
            r"全部|總共|至今)",
            t,
        )
    )
    if not has_period:
        return False
    stripped = t
    stripped = re.sub(r"\d{4}\s*年", " ", stripped)
    stripped = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*[日號]?", " ", stripped)
    stripped = re.sub(r"\d{1,2}\s*月(份)?", " ", stripped)
    stripped = re.sub(r"(十[一二]|[一二三四五六七八九十兩])\s*月(份)?", " ", stripped)
    stripped = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", " ", stripped)
    stripped = re.sub(r"\d{1,2}[/-]\d{1,2}", " ", stripped)
    stripped = re.sub(r"\d{1,2}\s*[號日]", " ", stripped)
    if re.search(r"\d", stripped):
        return False
    rest = re.sub(
        r"(花費|支出|消費|花了|花多少|多少錢|總花費|總支出|總消費|總共|合計|總額|統計|"
        r"明細|細項|清單|紀錄|記錄|記帳|帳|"
        r"收入|進帳|入帳|零用錢|薪水|薪資|"
        r"本月|這個月|當月|上個月|上月|下個月|下月|今天|今日|昨天|昨日|前天|"
        r"今年|去年|明年|本年|全部|至今|"
        r"查|看|幫我|一下|的|呢|嗎|請|我|想|要|知道|多少|幾|份)",
        " ",
        stripped,
    )
    rest = re.sub(r"[\s，,。．.！!？?、/~～\-]+", "", rest)
    return not rest


def looks_like_expense_write(text: str) -> bool:
    t = (text or "").strip()
    if not t or looks_like_stock_buy(t) or looks_like_stock_sell(t) or looks_like_stock_trades(t):
        return False
    if looks_like_expense_period_query(t):
        return False
    if re.search(r"(漲幅|跌幅|盈虧|報酬率|行事曆|股價)", t):
        return False
    if re.search(
        r"(總花費|花了?(多少|幾)|花多少錢|花費統計|明細|細項|圓餅|刪除\s*[#＃]?\s*\d+)",
        t,
    ):
        return False
    return bool(
        re.search(
            r"(記帳|記下|記錄|記一下|花了|支出|消費|"
            r"元|塊|"
            r"早餐|午餐|晚餐|消夜|宵夜|便當|捷運|咖啡|加油|停車|電影)",
            t,
        )
        or (
            # 多行／多筆金額 + 項目名
            len(re.findall(r"(?m)^\s*\S.+\s+\d{1,7}\s*(?:元|塊)?\s*$", t)) >= 2
        )
    )


def looks_like_calendar_list(text: str) -> bool:
    """查詢行程區間／月份口語。"""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"(行程|安排|行事曆|日程|行程表)", t):
        return True
    # 明確月份／日期區間（即使沒說「行程」也當查行事曆）
    if re.search(
        r"(\d{1,2}\s*月(?:份)?\s*[-~～到至]\s*\d{1,2}\s*月|"
        r"\d{1,2}\s*[-~～到至]\s*\d{1,2}\s*月|"
        r"[一二三四五六七八九十]+\s*月?(?:份)?\s*[-~～到至]\s*[一二三四五六七八九十]+\s*月|"
        r"從\s*.{0,8}(月|日).{0,4}(到|至|~|～).{0,8}(月|日)|"
        r"\d{1,2}[/-]\d{1,2}\s*[-~～到至]\s*\d{1,2}[/-]\d{1,2}|"
        r"\d{4}\s*年.{0,6}月.{0,6}(到|至|~|～).{0,10}月|"
        r"(今年|明年|去年).{0,6}月.{0,4}(到|至|~|～)|"
        r"月到明年|到明年\d{0,2}月|"
        r"上半年|下半年|第[一二三四]季|Q[1-4]|"
        r"(近|最近|過去|未來|接下來)\s*[兩三四五六七八九十\d]+\s*個?\s*月)",
        t,
    ):
        return True
    # 「10月有什麼」「查八月」
    if re.search(
        r"(有什麼|有哪些|有事嗎|查(一下|詢)?|看看?|列(出)?)",
        t,
    ) and re.search(
        r"(\d{1,2}\s*月|[一二三四五六七八九十]+\s*月|本月|這個月|下個月|上個月|"
        r"上半年|下半年|第[一二三四]季|Q[1-4])",
        t,
    ):
        return True
    return False


_TECH_TICKER = re.compile(r"[A-Za-z]{0,2}\d{3,6}[A-Za-z]?|[A-Za-z]{1,5}(?:[.-][A-Za-z]{1,2})?|[一-龥]{2,8}")


def extract_tech_query(text: str) -> str | None:
    """從「判斷 2330／判斷00947／分析台積電」抽出標的；沒有則 None。"""
    t = (text or "").strip()
    if not t or not re.search(r"(判斷|分析|技術面)", t):
        return None
    if _EXPENSE_DOMAIN.search(t) or _CALENDAR_DOMAIN.search(t):
        return None
    if re.search(r"(記帳|花費|行程|開會|會議|行事曆|股價|盈虧|報酬|我的股票|持股)", t):
        return None

    m = re.match(
        r"^(?:請)?(?:幫我)?"
        r"(?:做|來)?(?:個)?"
        r"(?:技術面)?"
        r"\s*(?:判斷|分析)\s*"
        r"(?:一下|看看?)?\s*"
        r"(?:技術面)?\s*"
        r"(.+)$",
        t,
    )
    query = ""
    if m:
        query = m.group(1).strip()
    else:
        m2 = re.match(
            r"^(.+?)\s*(?:的)?\s*(?:技術面)?\s*(?:判斷|分析)\s*$",
            t,
        )
        if m2:
            query = m2.group(1).strip()
    query = re.sub(r"^(的|一下|看看?)\s*", "", query).strip()
    query = re.sub(r"(技術面|判斷|分析)", "", query).strip()
    if _is_plausible_tech_target(query):
        return _normalize_tech_target(query)

    # 「判斷00947」「幫我分析一下 0050」：從整句再抽一次代號／名稱
    m3 = re.search(
        r"(?:判斷|分析|技術面)\s*(?:一下|看看?)?\s*" + _TECH_TICKER.pattern,
        t,
    )
    if m3:
        token = re.sub(r"^(?:判斷|分析|技術面)\s*(?:一下|看看?)?\s*", "", m3.group(0)).strip()
        if _is_plausible_tech_target(token):
            return _normalize_tech_target(token)
    return None


def _normalize_tech_target(query: str) -> str:
    q = (query or "").strip()
    # 常見多打一位：判斷009470 → 00947；判斷23300 → 2330
    if re.fullmatch(r"00\d{4}", q):
        return q[:5]
    if re.fullmatch(r"\d{5}", q) and q.endswith("0"):
        return q[:4]
    return q


def _is_plausible_tech_target(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    if re.fullmatch(r"[A-Za-z]{0,2}\d{3,6}[A-Za-z]?", q):
        return True
    if re.fullmatch(r"[A-Za-z]{1,5}(?:[.-][A-Za-z]{1,2})?", q):
        return True
    if re.fullmatch(r"[一-龥]{2,8}", q):
        return True
    return False


def classify_intent(text: str) -> Intent:
    """依優先序回傳單一意圖（互斥）。"""
    t = (text or "").strip()
    if not t:
        return Intent.OTHER

    # 1) 買股
    if looks_like_stock_buy(t):
        return Intent.STOCK_BUY

    # 1.5) 技術面買賣建議（須最先攔截「判斷00947」，避免落到查股價）
    if extract_tech_query(t):
        return Intent.STOCK_TECH

    # 1.6) 賣出持股／買賣紀錄（先於記帳與行程，避免「41塊」「8月到10月」被抢走）
    if looks_like_stock_sell(t):
        return Intent.STOCK_SELL
    if looks_like_stock_trades(t):
        return Intent.STOCK_TRADES

    # 2) 記帳刪除
    if re.search(
        r"(?:刪除|刪掉|刪去|移除|取消)\s*(?:這筆|該筆|記帳)?\s*[#＃]?\s*\d+",
        t,
    ) and (
        re.search(r"[#＃]|記帳|這筆|該筆|花費|支出|消費", t)
        or re.fullmatch(r"(?:刪除|刪掉|刪去|移除|取消)\s*[#＃]?\s*\d+\s*(?:這筆|該筆|筆)?", t)
    ):
        # 排除明確行程刪除
        if re.search(r"(行程|會議|開會|行事曆)", t) and not re.search(r"(記帳|花費|支出)", t):
            pass
        else:
            return Intent.EXPENSE_DELETE

    # 2.5) 收回某一筆／一般收入（先於記帳寫入，避免被記成正數花費）
    if looks_like_refund(t):
        return Intent.EXPENSE_REFUND
    if looks_like_income(t):
        return Intent.EXPENSE_INCOME

    # 3) 行程刪除
    if re.search(r"(移除|刪除|刪掉|刪去|取消)", t) and re.search(
        r"(行程|會議|開會|安排|行事曆|日程)", t
    ):
        return Intent.CALENDAR_DELETE

    # 4) 記帳圖／統計／明細（含「10月」「10月消費」，數字是月份不是金額）
    if looks_like_expense_period_query(t) and re.search(r"(圓餅圖|類別(統計)?圖|消費類別|類別占比|類別統計)", t):
        return Intent.EXPENSE_CHART
    if looks_like_expense_period_query(t):
        return Intent.EXPENSE_QUERY

    if re.search(r"(圓餅圖|類別(統計)?圖|消費類別|類別占比|類別統計)", t):
        return Intent.EXPENSE_CHART

    if re.search(
        r"(總花費|總支出|總消費|花了?(多少|幾)|花多少錢|花費統計|記帳統計|"
        r"總(共|計|額).*(花|支|消費)|(花|支|消費).*總(共|計|額)|"
        r"(今天|今日|昨天|本月|八月|今年).*(花費|支出|消費)|"
        r"(花費|支出|消費).*(多少|合計|總額|統計))",
        t,
    ) and not re.search(r"圓餅|圖", t):
        return Intent.EXPENSE_QUERY

    if re.search(
        r"(記帳|花費|支出|消費).*(明細|細項|清單|紀錄|記錄)|查.*記帳|(花|記)了?(什麼|甚麼)",
        t,
    ):
        return Intent.EXPENSE_QUERY

    # 5) 行程新增（含「10/10 下午五點 演唱會」這類口語）→ Google 行事曆
    if looks_like_calendar_add(t):
        return Intent.CALENDAR_ADD

    # 6) 行程列表（含 8-10月有什麼、下半年安排）
    if looks_like_calendar_list(t):
        return Intent.CALENDAR_LIST

    # 8) 股市專用
    if re.search(r"(今天|今日).*(查|看).*(股|股票)|查詢了哪些股票|查了哪些股票", t):
        return Intent.STOCK_TODAY_QUERIES

    if re.search(
        r"(持股|持倉|投資組合|portfolio|盈虧|報酬率|損益|未實現|"
        r"股票收益|持股收益|投資收益|今日收益|今天收益|"
        r"收益|獲利|賠錢|賠多少|賺多少|"
        r"我的股票|我的持股|目前股票|目前持股|持有股票|"
        r"股票淨利|持股淨利|淨值|未實現損益|"
        r"^(股票|持股)$)",
        t,
    ) and not re.search(r"買了|買入|賣了|結清", t):
        # 「今日花費」等記帳句不要誤判
        if re.search(r"(花費|記帳|支出|消費|午餐|早餐|晚餐)", t) and not re.search(
            r"(股|持股|投資|股票)", t
        ):
            pass
        else:
            return Intent.STOCK_PORTFOLIO

    if re.fullmatch(r"(大盤|加權|加權指數|今日台股|台股收盤|收盤整理)", t):
        return Intent.STOCK_MARKET

    if re.search(
        r"(漲幅|跌幅|漲跌幅|漲最多|跌最多|漲幅排行|跌幅排行)"
        r"|(今日|今天)?\s*台股\s*(漲|跌)",
        t,
    ):
        return Intent.STOCK_MOVERS

    # 9) 記帳寫入（有金額句）
    if looks_like_expense_write(t):
        return Intent.EXPENSE_WRITE

    # 10) 股價：嚴格
    if _EXPENSE_DOMAIN.search(t) or _CALENDAR_DOMAIN.search(t):
        return Intent.OTHER

    # 「這是最新股價嗎」這類追問 → OTHER，交給 Gemini
    if _STOCK_META_QUESTION.search(t):
        return Intent.OTHER
    if re.search(r"[嗎嘛呢？?]$", t):
        return Intent.OTHER

    # 純代號（可帶「股價」）
    m_code = re.fullmatch(
        r"(?:查詢?|看看?)?\s*"
        r"([A-Za-z]{0,2}\d{3,5}[A-Za-z]?)"
        r"\s*(?:的)?\s*(?:股價|報價|多少|現價)?",
        t,
    )
    if m_code and not _STOCK_BLOCK_AS_NAME.search(t):
        return Intent.STOCK_QUOTE

    # 「欣興股價」「台積電現價」— 名稱後必須有報價詞，避免整句被當公司名
    m_named = re.fullmatch(
        r"(?:查詢?|看看?)?\s*"
        r"([一-龥A-Za-z]{1,8})"
        r"\s*(?:的)?\s*(?:股價|報價|多少|現價)",
        t,
    )
    if m_named:
        query = m_named.group(1)
        if not _STOCK_BLOCK_AS_NAME.search(query) and not _STOCK_BLOCK_AS_NAME.search(t):
            return Intent.STOCK_QUOTE

    # 極短公司名単独出現（台積電、欣興）
    m_short = re.fullmatch(r"([一-龥A-Za-z]{2,6})", t)
    if m_short:
        query = m_short.group(1)
        if not _STOCK_BLOCK_AS_NAME.search(query) and not _EXPENSE_DOMAIN.search(t):
            return Intent.STOCK_QUOTE

    return Intent.OTHER
