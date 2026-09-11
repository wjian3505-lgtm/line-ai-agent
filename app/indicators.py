"""技術指標：Bollinger(20,2)、RSI(14)、MACD(12,26,9)。

優先 pandas-ta；匯入失敗則用等價 pandas 計算（RSI 為 Wilder 平滑）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

BB_LENGTH = 20
BB_STD = 2
RSI_LENGTH = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
LOOKBACK_DAYS = 10


def compute_recent_indicators(df) -> dict[str, Any]:
    """回傳最近 10 個交易日的指標。

    成功：{"ok": True, "rows": [dict, ...]}
    失敗：{"ok": False, "error": "..."}
    """
    try:
        import pandas as pd
    except ImportError:
        return {"ok": False, "error": "尚未安裝 pandas，無法計算技術指標。"}

    if df is None or getattr(df, "empty", True):
        return {"ok": False, "error": "沒有 K 線資料，無法計算技術指標。"}
    if "close" not in df.columns:
        return {"ok": False, "error": "K 線缺少收盤價，無法計算技術指標。"}
    if len(df) < MACD_SLOW + MACD_SIGNAL:
        return {
            "ok": False,
            "error": f"K 線只有 {len(df)} 根，計算 MACD 至少需要 {MACD_SLOW + MACD_SIGNAL} 根。",
        }

    work = df.copy()
    try:
        work = _apply_pandas_ta(work)
        source = "pandas-ta"
    except Exception as exc:  # noqa: BLE001
        logger.warning("pandas-ta 無法使用，改用 pandas 自算：%s", exc)
        try:
            work = _apply_pandas_fallback(work)
            source = "pandas"
        except Exception as inner:  # noqa: BLE001
            logger.exception("技術指標計算失敗")
            return {"ok": False, "error": f"技術指標計算失敗：{inner}"}

    cols = ["date", "close", "bb_upper", "bb_middle", "bb_lower", "rsi", "macd", "macd_signal", "macd_hist"]
    for col in cols:
        if col not in work.columns:
            return {"ok": False, "error": f"指標結果缺少欄位 {col}。"}

    recent = work.dropna(subset=["bb_middle", "rsi", "macd_hist"]).tail(LOOKBACK_DAYS)
    if len(recent) < 2:
        return {"ok": False, "error": "指標有效天數不足（需要至少 2 根完成的交易日）。"}

    rows: list[dict[str, Any]] = []
    for _, rec in recent.iterrows():
        dt = rec["date"]
        date_label = pd.Timestamp(dt).strftime("%Y-%m-%d") if pd.notna(dt) else ""
        rows.append(
            {
                "date": date_label,
                "close": _num(rec["close"]),
                "bb_upper": _num(rec["bb_upper"]),
                "bb_middle": _num(rec["bb_middle"]),
                "bb_lower": _num(rec["bb_lower"]),
                "rsi": _num(rec["rsi"]),
                "macd": _num(rec["macd"]),
                "macd_signal": _num(rec["macd_signal"]),
                "macd_hist": _num(rec["macd_hist"]),
            }
        )
    return {"ok": True, "rows": rows, "source": source}


def _apply_pandas_ta(df):
    import pandas_ta as ta

    close = df["close"]
    bb = ta.bbands(close, length=BB_LENGTH, std=BB_STD)
    rsi = ta.rsi(close, length=RSI_LENGTH)
    macd = ta.macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    if bb is None or rsi is None or macd is None:
        raise RuntimeError("pandas-ta 回傳空值")

    out = df.copy()
    out["bb_lower"] = _pick_col(bb, ("BBL", "lower"))
    out["bb_middle"] = _pick_col(bb, ("BBM", "mid", "middle"))
    out["bb_upper"] = _pick_col(bb, ("BBU", "upper"))
    out["rsi"] = rsi if getattr(rsi, "ndim", 1) == 1 else _pick_col(rsi, ("RSI",))
    out["macd"] = _pick_col(macd, ("MACD_", "MACD"))
    out["macd_signal"] = _pick_col(macd, ("MACDs", "signal"))
    out["macd_hist"] = _pick_col(macd, ("MACDh", "hist"))
    return out


def _apply_pandas_fallback(df):
    close = df["close"].astype(float)
    mid = close.rolling(BB_LENGTH, min_periods=BB_LENGTH).mean()
    sd = close.rolling(BB_LENGTH, min_periods=BB_LENGTH).std(ddof=0)
    rsi = _wilder_rsi(close, RSI_LENGTH)
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()

    out = df.copy()
    out["bb_middle"] = mid
    out["bb_upper"] = mid + BB_STD * sd
    out["bb_lower"] = mid - BB_STD * sd
    out["rsi"] = rsi
    out["macd"] = macd
    out["macd_signal"] = signal
    out["macd_hist"] = macd - signal
    return out


def _wilder_rsi(close, length: int):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _pick_col(frame, prefixes: tuple[str, ...]):
    if getattr(frame, "ndim", 1) == 1:
        return frame
    names = [str(c) for c in frame.columns]
    for prefix in prefixes:
        for name in names:
            if name.upper().startswith(prefix.upper()):
                return frame[name]
    raise KeyError(f"找不到指標欄位 {prefixes}，實際有 {names}")


def _num(value) -> float | None:
    try:
        if value is None:
            return None
        n = float(value)
        if n != n:  # NaN
            return None
        return round(n, 4)
    except (TypeError, ValueError):
        return None
