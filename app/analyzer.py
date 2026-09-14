"""技術面判讀：把 10 日指標 + v5.1 規則交給 Gemini，只依規則判定。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import settings
from .indicators import compute_recent_indicators
from .stock_data import fetch_ohlcv

logger = logging.getLogger(__name__)

DISCLAIMER = "本判定僅依據預設技術規則，非投資建議"

RULES_V51 = """【進場條件,需全部成立】
- 收盤價由下往上突破布林中軌
- RSI(14) > 50
- MACD 柱狀圖由負轉正(當日 > 0 且前一日 <= 0)
【出場條件,任一成立即符合】
- 收盤價跌破布林中軌
- RSI(14) < 45
- MACD 柱狀圖由正轉負"""

_VERDICTS = {"符合進場", "符合出場", "觀望"}
_ACTIONS = {
    "符合進場": "建議買入",
    "符合出場": "建議賣出",
    "觀望": "建議觀望",
}
_CONFIDENCE = {"高", "中", "低"}
_TIMEOUT_MS = 30_000


def run_analysis(query: str) -> dict[str, Any]:
    """完整流程：日 K → 指標 → 規則判定買賣。Gemini 只補風險說明。"""
    bars = fetch_ohlcv(query)
    if not bars.get("ok"):
        return {"ok": False, "error": bars.get("error") or "讀取行情失敗。"}

    metrics = compute_recent_indicators(bars["df"])
    if not metrics.get("ok"):
        return {"ok": False, "error": metrics.get("error") or "計算指標失敗。"}

    payload = apply_rules_v51(metrics["rows"])
    judged = judge_with_gemini(
        symbol=str(bars["symbol"]),
        name=str(bars.get("name") or ""),
        market=str(bars.get("market") or ""),
        rows=metrics["rows"],
        local_payload=payload,
    )
    if judged.get("ok") and judged.get("data"):
        extra = judged["data"]
        if extra.get("risk_note") and extra["risk_note"] != "—":
            payload["risk_note"] = extra["risk_note"]
        if extra.get("confidence") in _CONFIDENCE:
            payload["confidence"] = extra["confidence"]

    symbol = str(bars["symbol"])
    name = str(bars.get("name") or "")
    title = f"{symbol} {name}".strip()
    alt = format_alt_text(title, payload)
    return {
        "ok": True,
        "symbol": symbol,
        "name": name,
        "market": bars.get("market"),
        "payload": payload,
        "alt_text": alt,
        "indicator_source": metrics.get("source"),
    }


def apply_rules_v51(rows: list[dict]) -> dict[str, Any]:
    """依 v5.1 規則用最近兩根指標判定買／賣／觀望（不依賴模型）。"""
    if not rows or len(rows) < 2:
        return {
            "verdict": "觀望",
            "action": "建議觀望",
            "matched_rules": [],
            "failed_rules": ["指標天數不足，無法比對突破／轉折"],
            "risk_note": "資料不足，暫不給買賣建議。",
            "confidence": "低",
            "snapshot": {},
        }
    prev, curr = rows[-2], rows[-1]
    entry_checks = [
        ("收盤價由下往上突破布林中軌", _crossed_up(prev, curr, "close", "bb_middle")),
        ("RSI(14) > 50", _gt(curr.get("rsi"), 50)),
        ("MACD 柱狀圖由負轉正(當日 > 0 且前一日 <= 0)", _hist_neg_to_pos(prev, curr)),
    ]
    exit_checks = [
        ("收盤價跌破布林中軌", _crossed_down(prev, curr, "close", "bb_middle")),
        ("RSI(14) < 45", _lt(curr.get("rsi"), 45)),
        ("MACD 柱狀圖由正轉負", _hist_pos_to_neg(prev, curr)),
    ]
    entry_hit = [name for name, ok in entry_checks if ok]
    entry_miss = [name for name, ok in entry_checks if not ok]
    exit_hit = [name for name, ok in exit_checks if ok]
    exit_miss = [name for name, ok in exit_checks if not ok]
    entry_ok = len(entry_miss) == 0
    exit_ok = len(exit_hit) > 0

    if entry_ok and exit_ok:
        verdict = "觀望"
        matched, failed = entry_hit + exit_hit, []
        confidence = "中"
    elif entry_ok:
        verdict = "符合進場"
        matched, failed = entry_hit, exit_miss
        confidence = "高"
    elif exit_ok:
        verdict = "符合出場"
        matched, failed = exit_hit, entry_miss
        confidence = "高"
    else:
        verdict = "觀望"
        matched, failed = [], entry_miss + [n for n in exit_miss if n not in entry_miss]
        confidence = "中"

    snap = {
        "close": curr.get("close"),
        "bb_middle": curr.get("bb_middle"),
        "rsi": curr.get("rsi"),
        "macd_hist": curr.get("macd_hist"),
        "date": curr.get("date"),
    }
    return {
        "verdict": verdict,
        "action": _ACTIONS[verdict],
        "matched_rules": matched,
        "failed_rules": failed,
        "risk_note": _default_risk(snap, verdict),
        "confidence": confidence,
        "snapshot": snap,
    }


def _num_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        n = float(value)
        if n != n:
            return None
        return n
    except (TypeError, ValueError):
        return None


def _gt(value: Any, threshold: float) -> bool:
    n = _num_or_none(value)
    return n is not None and n > threshold


def _lt(value: Any, threshold: float) -> bool:
    n = _num_or_none(value)
    return n is not None and n < threshold


def _crossed_up(prev: dict, curr: dict, price_key: str, line_key: str) -> bool:
    pc, cc = _num_or_none(prev.get(price_key)), _num_or_none(curr.get(price_key))
    pl, cl = _num_or_none(prev.get(line_key)), _num_or_none(curr.get(line_key))
    if None in (pc, cc, pl, cl):
        return False
    return pc <= pl and cc > cl


def _crossed_down(prev: dict, curr: dict, price_key: str, line_key: str) -> bool:
    pc, cc = _num_or_none(prev.get(price_key)), _num_or_none(curr.get(price_key))
    pl, cl = _num_or_none(prev.get(line_key)), _num_or_none(curr.get(line_key))
    if None in (pc, cc, pl, cl):
        return False
    return pc >= pl and cc < cl


def _hist_neg_to_pos(prev: dict, curr: dict) -> bool:
    ph, ch = _num_or_none(prev.get("macd_hist")), _num_or_none(curr.get("macd_hist"))
    return ph is not None and ch is not None and ph <= 0 and ch > 0


def _hist_pos_to_neg(prev: dict, curr: dict) -> bool:
    ph, ch = _num_or_none(prev.get("macd_hist")), _num_or_none(curr.get("macd_hist"))
    return ph is not None and ch is not None and ph > 0 and ch <= 0


def _default_risk(snap: dict[str, Any], verdict: str) -> str:
    close = snap.get("close")
    mid = snap.get("bb_middle")
    rsi = snap.get("rsi")
    hist = snap.get("macd_hist")
    bits = [f"收盤 {close}", f"布林中軌 {mid}", f"RSI {rsi}", f"MACD柱 {hist}"]
    if verdict == "觀望":
        return "進場或出場條件尚未齊備，" + "，".join(bits) + "。暫不建議追價。"
    return "，".join(bits) + "。"


def judge_with_gemini(
    *,
    symbol: str,
    name: str,
    market: str,
    rows: list[dict],
    local_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not settings.gemini_api_key:
        return {"ok": False, "error": "尚未設定 GEMINI_API_KEY，無法進行技術面判讀。"}
    if not rows:
        return {"ok": False, "error": "沒有指標數值可送判讀。"}

    prompt = _build_prompt(symbol, name, market, rows, local_payload)
    last_err = ""
    for attempt in range(2):
        raw, err = _call_gemini(prompt)
        if err:
            if _is_quota_error(err):
                return {"ok": False, "error": "AI 判讀額度已滿，請稍後再試"}
            last_err = err
            continue
        parsed = _parse_json(raw)
        if parsed is None:
            last_err = "JSON 解析失敗"
            logger.warning("Gemini 回傳無法解析（第 %d 次）：%s", attempt + 1, (raw or "")[:240])
            continue
        data = _normalize_payload(parsed)
        if data is None:
            last_err = "JSON 欄位不完整"
            continue
        return {"ok": True, "data": data}

    return {"ok": False, "error": f"技術面判讀暫時無法完成，請稍後再試。"}


def format_alt_text(title: str, payload: dict[str, Any]) -> str:
    action = payload.get("action") or _ACTIONS.get(str(payload.get("verdict")), "建議觀望")
    matched = "、".join(payload.get("matched_rules") or []) or "無"
    failed = "、".join(payload.get("failed_rules") or []) or "無"
    snap = payload.get("snapshot") or {}
    snap_line = ""
    if snap:
        snap_line = (
            f"指標：收盤 {snap.get('close')}｜中軌 {snap.get('bb_middle')}｜"
            f"RSI {snap.get('rsi')}｜MACD柱 {snap.get('macd_hist')}\n"
        )
    return (
        f"{title} 技術面判定\n"
        f"{action}\n"
        f"{snap_line}"
        f"命中：{matched}\n"
        f"未中：{failed}\n"
        f"風險：{payload.get('risk_note') or '—'}\n"
        f"信心度：{payload.get('confidence')}\n"
        f"{DISCLAIMER}"
    )


def _build_prompt(
    symbol: str,
    name: str,
    market: str,
    rows: list[dict],
    local_payload: dict[str, Any] | None = None,
) -> str:
    market_label = "台股" if market == "TW" else ("美股" if market == "US" else market)
    lines = [
        "你是技術分析規則引擎，只能依據下方「交易規則(v5.1)」與「最近 10 個交易日指標」判定。",
        "禁止自行預測漲跌、禁止發明規則、禁止使用規則以外的指標或消息面。",
        "進場條件需全部成立才可判定「符合進場」。出場條件任一成立即「符合出場」。",
        "若進場與出場同時成立，判定「觀望」，並在 risk_note 說明衝突。",
        "若兩者皆不成立，判定「觀望」。",
        "只輸出 JSON，不要 Markdown、不要註解。",
        "",
        f"標的：{symbol} {name}（{market_label}）",
        "",
        "交易規則(v5.1)：",
        RULES_V51,
        "",
        "最近 10 個交易日指標（由舊到新，最後一列為最新交易日）：",
        json.dumps(rows, ensure_ascii=False),
        "",
        "JSON schema：",
        json.dumps(
            {
                "verdict": "符合進場 | 符合出場 | 觀望",
                "matched_rules": ["..."],
                "failed_rules": ["..."],
                "risk_note": "...",
                "confidence": "高 | 中 | 低",
            },
            ensure_ascii=False,
        ),
        "matched_rules / failed_rules 請用規則原文或可對應的短句。risk_note 以繁體中文、一兩句話說明風險。",
        "買賣結論已由規則引擎算出，請沿用，不要改 verdict。risk_note 只補充風險，不要改成另一個買賣方向。",
    ]
    if local_payload:
        lines.extend(
            [
                "",
                "規則引擎既有結論（請沿用 verdict）：",
                json.dumps(
                    {
                        "verdict": local_payload.get("verdict"),
                        "action": local_payload.get("action"),
                        "matched_rules": local_payload.get("matched_rules"),
                        "failed_rules": local_payload.get("failed_rules"),
                    },
                    ensure_ascii=False,
                ),
            ]
        )
    return "\n".join(lines)


def _call_gemini(prompt: str) -> tuple[str | None, str | None]:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, "尚未安裝 google-genai"

    try:
        client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=_TIMEOUT_MS),
        )
        response = client.models.generate_content(
            model=settings.gemini_model or "gemini-3.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        if not text:
            return None, "Gemini 回傳空白"
        return text, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gemini 技術面判讀失敗")
        return None, str(exc)


def _is_quota_error(err: str) -> bool:
    t = (err or "").upper()
    return any(k in t for k in ("429", "RESOURCE_EXHAUSTED", "QUOTA", "RATE LIMIT"))


def _parse_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _normalize_payload(data: dict) -> dict[str, Any] | None:
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in _VERDICTS:
        return None
    confidence = str(data.get("confidence") or "中").strip()
    if confidence not in _CONFIDENCE:
        confidence = "中"
    matched = _as_str_list(data.get("matched_rules"))
    failed = _as_str_list(data.get("failed_rules"))
    risk = str(data.get("risk_note") or "").strip() or "—"
    return {
        "verdict": verdict,
        "action": _ACTIONS[verdict],
        "matched_rules": matched,
        "failed_rules": failed,
        "risk_note": risk,
        "confidence": confidence,
    }


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[、,，;；\n]", value) if p.strip()]
        return parts
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []
