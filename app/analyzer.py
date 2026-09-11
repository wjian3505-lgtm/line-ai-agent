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
_CONFIDENCE = {"高", "中", "低"}
_TIMEOUT_MS = 30_000


def run_analysis(query: str) -> dict[str, Any]:
    """完整流程：日 K → 指標 → Gemini JSON。失敗時帶 error。"""
    bars = fetch_ohlcv(query)
    if not bars.get("ok"):
        return {"ok": False, "error": bars.get("error") or "讀取行情失敗。"}

    metrics = compute_recent_indicators(bars["df"])
    if not metrics.get("ok"):
        return {"ok": False, "error": metrics.get("error") or "計算指標失敗。"}

    judged = judge_with_gemini(
        symbol=str(bars["symbol"]),
        name=str(bars.get("name") or ""),
        market=str(bars.get("market") or ""),
        rows=metrics["rows"],
    )
    if not judged.get("ok"):
        return judged

    payload = judged["data"]
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


def judge_with_gemini(*, symbol: str, name: str, market: str, rows: list[dict]) -> dict[str, Any]:
    if not settings.gemini_api_key:
        return {"ok": False, "error": "尚未設定 GEMINI_API_KEY，無法進行技術面判讀。"}
    if not rows:
        return {"ok": False, "error": "沒有指標數值可送判讀。"}

    prompt = _build_prompt(symbol, name, market, rows)
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
    matched = "、".join(payload.get("matched_rules") or []) or "無"
    failed = "、".join(payload.get("failed_rules") or []) or "無"
    return (
        f"{title} 技術面判定\n"
        f"判定：{payload.get('verdict')}\n"
        f"命中：{matched}\n"
        f"未中：{failed}\n"
        f"風險：{payload.get('risk_note') or '—'}\n"
        f"信心度：{payload.get('confidence')}\n"
        f"{DISCLAIMER}"
    )


def _build_prompt(symbol: str, name: str, market: str, rows: list[dict]) -> str:
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
    ]
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
