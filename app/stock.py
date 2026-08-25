"""Fugle 富果行情資料存取：個股即時報價與每日收盤整理。

支援「代號」或「公司名稱」查詢；名稱會透過 Fugle 股票清單自動對應代號，
避免 AI 猜錯代號或只會查自選股。
"""
import logging
import re
import time
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from .config import settings

logger = logging.getLogger(__name__)

# 常見別名 / 口語名稱 → 正式簡稱或代號（補強 Fugle 清單不完全吻合時）
_ALIASES = {
    "台積電": "2330",
    "台積": "2330",
    "聯發科": "2454",
    "聯發": "2454",
    "鴻海": "2317",
    "欣興": "3037",
    "大盤": "IX0001",
    "加權": "IX0001",
    "加權指數": "IX0001",
    "台股": "IX0001",
}


@lru_cache
def _client():
    """延遲初始化 Fugle RestClient（避免無金鑰時匯入即失敗）。"""
    if not settings.fugle_api_key:
        raise RuntimeError("尚未設定 FUGLE_API_KEY，無法查詢股市資料。")
    from fugle_marketdata import RestClient

    return RestClient(api_key=settings.fugle_api_key)


def _fmt_change(change, change_percent) -> str:
    try:
        arrow = "▲" if change > 0 else ("▼" if change < 0 else "－")
        return f"{arrow}{abs(change):.2f} ({change_percent:+.2f}%)"
    except (TypeError, ValueError):
        return "-"


@lru_cache(maxsize=1)
def _load_ticker_maps() -> Tuple[Dict[str, str], Dict[str, str]]:
    """載入上市/上櫃股票清單，建立 代號↔名稱 對照（程序生命週期內快取）。"""
    by_symbol: Dict[str, str] = {}
    by_name: Dict[str, str] = {}
    stock = _client().stock
    for exchange in ("TWSE", "TPEx"):
        try:
            resp = stock.intraday.tickers(type="EQUITY", exchange=exchange)
        except Exception:  # noqa: BLE001
            logger.exception("載入 %s 股票清單失敗", exchange)
            continue
        rows = resp.get("data") if isinstance(resp, dict) else None
        if not rows:
            continue
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            name = str(row.get("name") or "").strip()
            if not symbol or not name:
                continue
            by_symbol[symbol] = name
            by_name[name] = symbol
            # 也支援去掉常見前後綴後的近似比對 key
            compact = re.sub(r"\s+", "", name)
            by_name.setdefault(compact, symbol)
    logger.info("已載入股票清單：%d 檔", len(by_symbol))
    return by_symbol, by_name


def resolve_symbol(query: str) -> Tuple[Optional[str], Optional[str]]:
    """把使用者輸入（代號或名稱）解析成正式代號。

    Returns:
        (symbol, matched_name)；解析失敗回 (None, None)。
    """
    q = (query or "").strip()
    if not q:
        return None, None

    # 純代號（含指數 IX0001、ETF 0050 等）
    if re.fullmatch(r"[A-Za-z]{0,2}\d{3,5}[A-Za-z]?", q):
        return q.upper() if q[0].isalpha() else q, None

    # 別名表
    if q in _ALIASES:
        return _ALIASES[q], q

    by_symbol, by_name = _load_ticker_maps()

    # 精確名稱
    if q in by_name:
        return by_name[q], q

    # 包含式模糊：名稱包含 query，或 query 包含名稱
    # 優先較短、較精準的命中，避免「台」誤中太多
    candidates: List[Tuple[int, str, str]] = []
    for name, symbol in by_name.items():
        if q == name:
            return symbol, name
        if q in name or name in q:
            # 分數：長度差越小越好
            score = abs(len(name) - len(q))
            candidates.append((score, symbol, name))
    if candidates:
        candidates.sort(key=lambda x: (x[0], len(x[2])))
        _, symbol, name = candidates[0]
        return symbol, name

    return None, None


def get_stock_quote(symbol: str, retries: int = 2) -> dict:
    """查詢單一台股的即時報價（含瞬間錯誤自動重試）。

    Args:
        symbol: 台股代號，例如台積電為 "2330"。也可傳公司名稱，會自動解析。
        retries: 遇到 API 瞬間錯誤時的重試次數。

    Returns:
        含收盤/現價、漲跌、成交量等資訊的字典；查無資料時回傳 error。
    """
    resolved, matched_name = resolve_symbol(symbol)
    if not resolved:
        return {
            "symbol": symbol,
            "error": f"找不到「{symbol}」對應的台股代號，請改傳 4 碼代號（例如 2330）。",
        }
    symbol = resolved

    data = None
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            stock = _client().stock
            data = stock.intraday.quote(symbol=symbol)
            last_err = None
            break
        except Exception as exc:  # noqa: BLE001 對外統一回錯誤訊息
            last_err = exc
            logger.exception("查詢 %s 第 %d 次失敗", symbol, attempt + 1)
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))

    if last_err is not None:
        return {"symbol": symbol, "error": str(last_err)}

    if not isinstance(data, dict) or data.get("statusCode"):
        hint = f"（對應名稱：{matched_name}）" if matched_name else ""
        return {"symbol": symbol, "error": f"查無 {symbol}{hint} 的報價資料。"}

    return {
        "symbol": data.get("symbol", symbol),
        "name": data.get("name") or matched_name,
        "price": data.get("lastPrice") or data.get("closePrice"),
        "open": data.get("openPrice"),
        "high": data.get("highPrice"),
        "low": data.get("lowPrice"),
        "change": data.get("change"),
        "change_percent": data.get("changePercent"),
        "volume": data.get("total", {}).get("tradeVolume")
        if isinstance(data.get("total"), dict)
        else data.get("tradeVolume"),
        "date": data.get("date"),
    }


def format_quote(q: dict) -> str:
    """把報價字典整理成適合 LINE 顯示的文字。"""
    if q.get("error"):
        return f"⚠️ 查詢 {q.get('symbol')} 失敗：{q['error']}"
    name = q.get("name") or ""
    header = f"{q['symbol']} {name}".strip()
    return (
        f"📈 {header}\n"
        f"現價：{q.get('price')}  {_fmt_change(q.get('change'), q.get('change_percent'))}\n"
        f"開/高/低：{q.get('open')} / {q.get('high')} / {q.get('low')}\n"
        f"成交量：{q.get('volume')}\n"
        f"資料日期：{q.get('date')}"
    )


def get_market_summary() -> str:
    """整理當日台股概況：大盤加權指數 + 自選股清單收盤情形。"""
    lines: List[str] = ["📊 今日台股收盤整理"]

    # 大盤加權指數（IX0001）
    index = get_stock_quote("IX0001")
    if not index.get("error") and index.get("price") is not None:
        lines.append(
            f"加權指數：{index.get('price')}  {_fmt_change(index.get('change'), index.get('change_percent'))}"
        )

    lines.append("—" * 8)
    for sym in settings.stock_watchlist:
        q = get_stock_quote(sym)
        if q.get("error"):
            lines.append(f"{sym}：{q['error']}")
            continue
        name = q.get("name") or ""
        lines.append(
            f"{q['symbol']} {name}  {q.get('price')}  {_fmt_change(q.get('change'), q.get('change_percent'))}"
        )

    return "\n".join(lines)


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("+", "")
    if not text or text in {"-", "--", "---", "除權息", "除權", "除息"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fetch_twse_day_rows() -> List[dict]:
    import requests

    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _fetch_tpex_day_rows() -> List[dict]:
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    resp = requests.get(url, timeout=30, verify=False)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _collect_change_percent_rows(market: str = "ALL") -> Tuple[str, List[dict]]:
    """彙整上市/上櫃普通股漲跌幅。"""
    rows: List[dict] = []
    dates: List[str] = []
    market = (market or "ALL").upper()

    if market in ("ALL", "TSE", "TWSE", "上市"):
        for r in _fetch_twse_day_rows():
            code = str(r.get("Code") or "").strip()
            if not re.fullmatch(r"\d{4}", code):
                continue
            close = _safe_float(r.get("ClosingPrice"))
            change = _safe_float(r.get("Change"))
            if close is None or change is None or close <= 0:
                continue
            prev = close - change
            if prev == 0:
                continue
            pct = change / prev * 100
            dates.append(str(r.get("Date") or ""))
            rows.append(
                {
                    "symbol": code,
                    "name": r.get("Name") or "",
                    "price": close,
                    "change": change,
                    "change_percent": pct,
                    "market": "上市",
                }
            )

    if market in ("ALL", "OTC", "TPEX", "上櫃"):
        for r in _fetch_tpex_day_rows():
            code = str(r.get("SecuritiesCompanyCode") or "").strip()
            if not re.fullmatch(r"\d{4}", code):
                continue
            close = _safe_float(r.get("Close"))
            change = _safe_float(r.get("Change"))
            if close is None or change is None or close <= 0:
                continue
            prev = close - change
            if prev == 0:
                continue
            pct = change / prev * 100
            dates.append(str(r.get("Date") or ""))
            rows.append(
                {
                    "symbol": code,
                    "name": r.get("CompanyName") or "",
                    "price": close,
                    "change": change,
                    "change_percent": pct,
                    "market": "上櫃",
                }
            )

    date_label = max(dates) if dates else ""
    if re.fullmatch(r"\d{7}", date_label):
        y = int(date_label[:3]) + 1911
        date_label = f"{y}/{date_label[3:5]}/{date_label[5:7]}"
    return date_label, rows


def get_stock_movers_rows(
    direction: str = "up", limit: int = 10, market: str = "ALL"
) -> Tuple[str, str, List[dict]]:
    """回傳 (title, date_label, rows)。供 Flex 使用。"""
    direction = (direction or "up").lower().strip()
    if direction in ("漲", "漲幅", "gainers", "gainer"):
        direction = "up"
    if direction in ("跌", "跌幅", "losers", "loser"):
        direction = "down"
    if direction not in ("up", "down"):
        return "錯誤", "", []

    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 10
    limit_n = max(1, min(limit_n, 30))

    try:
        date_label, rows = _collect_change_percent_rows(market)
    except Exception as exc:  # noqa: BLE001
        logger.exception("取得公開漲跌幅排行失敗：%s", exc)
        return "無法取得資料", "", []

    if not rows:
        return "無資料", "", []

    rows.sort(key=lambda x: x["change_percent"], reverse=(direction == "up"))
    title = "漲幅前 10" if direction == "up" else "跌幅前 10"
    market_label = {
        "ALL": "上市+上櫃",
        "TSE": "上市",
        "TWSE": "上市",
        "OTC": "上櫃",
        "TPEX": "上櫃",
    }.get((market or "ALL").upper(), "上市+上櫃")
    return (
        f"台股{title}（{market_label}）",
        f"{date_label or '最新交易日'} · 收盤公開資料",
        rows[:limit_n],
    )


def get_stock_movers(direction: str = "up", limit: int = 10, market: str = "ALL") -> str:
    """列出台股漲幅或跌幅前 N 名。"""
    title, date_label, rows = get_stock_movers_rows(direction, limit, market)
    if not rows:
        return "目前抓不到漲跌幅排行資料，請稍後再試。"
    lines = [f"📊 {title}", f"資料日期：{date_label}", "—" * 8]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. {row['symbol']} {row['name']}  "
            f"{row['price']}  {_fmt_change(row['change'], row['change_percent'])}"
            f"  [{row.get('market') or ''}]"
        )
    return "\n".join(lines)


def get_stock_movers_both(limit: int = 10, market: str = "ALL") -> str:
    """一次回傳漲幅前 N 與跌幅前 N。"""
    up = get_stock_movers("up", limit=limit, market=market)
    down = get_stock_movers("down", limit=limit, market=market)
    return up + "\n\n" + down


def get_stock_movers_both_data(
    limit: int = 10, market: str = "ALL"
) -> List[Tuple[str, str, List[dict], str]]:
    """[(title, date_label, rows, direction), ...]"""
    out: List[Tuple[str, str, List[dict], str]] = []
    for direction in ("up", "down"):
        title, date_label, rows = get_stock_movers_rows(direction, limit, market)
        out.append((title, date_label, rows, direction))
    return out

