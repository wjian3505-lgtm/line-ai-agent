"""技術面判定用 K 線資料：台股 Fugle、美股 yfinance。

統一回傳 pandas DataFrame：date, open, high, low, close, volume。
錯誤以明確訊息回傳，不往外拋未處理例外。
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from .config import settings
from .stock import resolve_symbol

logger = logging.getLogger(__name__)

_BARS = 60
_MIN_BARS = 30
_FETCH_TIMEOUT = 20

_TW_CODE = re.compile(r"^[A-Za-z]{0,2}\d{3,5}[A-Za-z]?$")
_US_CODE = re.compile(r"^[A-Za-z]{1,5}(?:[.-][A-Za-z]{1,2})?$")


def fetch_ohlcv(query: str) -> dict[str, Any]:
    """依代碼／名稱抓最近約 60 根日 K。

    Returns:
        成功：{"ok": True, "symbol", "name", "market", "df"}
        失敗：{"ok": False, "error": "..."}
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "請提供股票代碼或名稱，例如 2330、台積電、AAPL。"}

    try:
        if _US_CODE.fullmatch(q) and not _TW_CODE.fullmatch(q):
            return _fetch_us(q.upper())
        return _fetch_tw(q)
    except Exception as exc:  # noqa: BLE001
        logger.exception("抓取 K 線失敗：%s", q)
        return {"ok": False, "error": f"讀取行情失敗：{_friendly_exc(exc)}"}


def _fetch_tw(query: str) -> dict[str, Any]:
    if not settings.fugle_api_key:
        return {"ok": False, "error": "尚未設定 FUGLE_API_KEY，無法抓取台股日 K。"}

    symbol, matched_name = resolve_symbol(query)
    if not symbol:
        return {
            "ok": False,
            "error": f"找不到「{query}」對應的台股代號，請改傳 4 碼代號（例如 2330）或英文代碼（例如 AAPL）。",
        }
    if symbol.upper().startswith("IX"):
        return {"ok": False, "error": "指數不支援技術面判定，請改傳個股代號。"}

    import requests

    end = date.today()
    start = end - timedelta(days=180)
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{symbol}"
    try:
        resp = requests.get(
            url,
            params={"from": start.isoformat(), "to": end.isoformat()},
            headers={"X-API-KEY": settings.fugle_api_key},
            timeout=_FETCH_TIMEOUT,
        )
    except requests.Timeout:
        return {"ok": False, "error": f"Fugle 查詢 {symbol} 逾時，請稍後再試。"}
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Fugle 連線失敗：{exc}"}

    if resp.status_code == 401:
        return {"ok": False, "error": "Fugle API 金鑰無效，請檢查 FUGLE_API_KEY。"}
    if resp.status_code == 404:
        return {"ok": False, "error": f"找不到代號 {symbol} 的日 K 資料。"}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"Fugle 回傳錯誤（HTTP {resp.status_code}），請稍後再試。"}

    try:
        payload = resp.json()
    except ValueError:
        return {"ok": False, "error": f"Fugle 回傳無法解析，無法判定 {symbol}。"}

    if isinstance(payload, dict) and payload.get("statusCode"):
        msg = payload.get("message") or "查無資料"
        return {"ok": False, "error": f"找不到 {symbol} 的日 K：{msg}"}

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not rows:
        return {"ok": False, "error": f"{symbol} 近日沒有足夠的交易日資料，請換一個代碼或下個交易日再試。"}

    name = (
        (payload.get("name") if isinstance(payload, dict) else None)
        or matched_name
        or _tw_name(symbol)
        or query
    )
    df = _rows_to_df(rows)
    checked = _validate_df(df, symbol)
    if checked.get("error"):
        return {"ok": False, "error": checked["error"]}
    return {
        "ok": True,
        "symbol": symbol,
        "name": str(name),
        "market": "TW",
        "df": checked["df"],
    }


def _fetch_us(symbol: str) -> dict[str, Any]:
    try:
        import yfinance as yf
    except ImportError:
        return {"ok": False, "error": "尚未安裝 yfinance，無法抓取美股日 K。"}

    try:
        ticker = yf.Ticker(symbol)
        try:
            hist = ticker.history(
                period="6mo",
                interval="1d",
                auto_adjust=False,
                timeout=_FETCH_TIMEOUT,
            )
        except TypeError:
            hist = ticker.history(period="6mo", interval="1d", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "timed out" in msg or "timeout" in msg:
            return {"ok": False, "error": f"美股 {symbol} 查詢逾時，請稍後再試。"}
        return {"ok": False, "error": f"找不到「{symbol}」的美股資料：{_friendly_exc(exc)}"}

    if hist is None or hist.empty:
        return {"ok": False, "error": f"找不到「{symbol}」對應的美股代碼，或近日沒有交易資料。"}

    work = hist.reset_index()
    date_col = "Date" if "Date" in work.columns else work.columns[0]
    rename = {}
    for src, dst in (
        (date_col, "date"),
        ("Open", "open"),
        ("High", "high"),
        ("Low", "low"),
        ("Close", "close"),
        ("Volume", "volume"),
    ):
        if src in work.columns:
            rename[src] = dst
    work = work.rename(columns=rename)
    df = _normalize_df(work)
    name = _us_name(ticker, symbol)
    checked = _validate_df(df, symbol)
    if checked.get("error"):
        return {"ok": False, "error": checked["error"]}
    return {
        "ok": True,
        "symbol": symbol,
        "name": name,
        "market": "US",
        "df": checked["df"],
    }


def _tw_name(symbol: str) -> str:
    try:
        from .stock import _load_ticker_maps

        by_symbol, _ = _load_ticker_maps()
        return str(by_symbol.get(symbol) or "")
    except Exception:  # noqa: BLE001
        return ""


def _us_name(ticker: Any, symbol: str) -> str:
    try:
        info = ticker.get_info() if hasattr(ticker, "get_info") else (ticker.info or {})
        return str(info.get("shortName") or info.get("longName") or symbol)
    except Exception:  # noqa: BLE001
        return symbol


def _rows_to_df(rows: list) -> Any:
    import pandas as pd

    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        records.append(
            {
                "date": row.get("date"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            }
        )
    return _normalize_df(pd.DataFrame.from_records(records))


def _normalize_df(df: Any) -> Any:
    import pandas as pd

    need = ["date", "open", "high", "low", "close", "volume"]
    for col in need:
        if col not in df.columns:
            df[col] = None
    out = df[need].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce", utc=True).dt.tz_convert(None).dt.normalize()
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date")
    out = out.reset_index(drop=True)
    return out


def _validate_df(df: Any, symbol: str) -> dict[str, Any]:
    if df is None or df.empty:
        return {"error": f"{symbol} 近日沒有可用的日 K 資料。"}
    df = df.tail(_BARS).reset_index(drop=True)
    if len(df) < _MIN_BARS:
        return {
            "error": f"{symbol} 可取得的交易日只有 {len(df)} 根（至少需要 {_MIN_BARS} 根），資料不足無法判定。",
        }
    return {"df": df}


def _friendly_exc(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.split("\n")[0][:160]
