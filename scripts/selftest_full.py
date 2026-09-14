"""完整功能回歸：記帳／行程／股市／Flex 序列化／健康檢查。"""
from __future__ import annotations

import traceback

from linebot.v3.messaging import FlexContainer, FlexMessage

from app import flex_ui
from app.agent import _should_defer_expense_to_b, try_route_a
from app.calendar_google import (
    calendar_authorized,
    calendar_configured,
    list_events_rows_for_period,
    resolve_calendar_range,
)
from app.db import init_db, session_scope
from app.expenses import (
    delete_expense_record,
    parse_expense_utterance,
    record_expense,
    summarize_expenses_period,
)
from app.main import _build_line_messages, app
from app.portfolio import portfolio_summary_data
from app.stock import (
    format_quote,
    get_market_summary,
    get_stock_movers_both_data,
    get_stock_quote,
)

passed = 0
failed = 0
errors: list[str] = []
UID = "selftest-full-user"


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"OK  {name}")
    else:
        failed += 1
        msg = f"{name}: {detail}"
        errors.append(msg)
        print(f"FAIL {name} :: {detail}")


def flex_ok(reply) -> bool:
    if not reply or not reply.flex:
        return False
    try:
        msgs = _build_line_messages(reply)
        d = msgs[0].to_dict()
        c = d.get("contents") or {}
        # bubble 需有 body；carousel 需有 contents 陣列
        if c.get("type") == "bubble":
            return bool(c.get("body") or c.get("header"))
        if c.get("type") == "carousel":
            kids = c.get("contents") or []
            return bool(kids) and all(k.get("body") or k.get("header") for k in kids)
        return False
    except Exception:  # noqa: BLE001
        return False


def a_case(text: str, expect_type: str | None = "bubble") -> None:
    try:
        r = try_route_a(UID, text)
        if expect_type is None:
            check(f"A miss→B: {text}", r is None)
            return
        ok = r is not None and flex_ok(r) and (r.flex or {}).get("type") == expect_type
        detail = ""
        if r:
            detail = f"type={(r.flex or {}).get('type')} alt={r.text[:60]!r}"
        else:
            detail = "no reply"
        check(f"A {text}", ok, detail)
    except Exception:  # noqa: BLE001
        check(f"A {text}", False, traceback.format_exc(limit=2))


def main() -> int:
    init_db()
    print("======== 1. Flex 基礎 ========")
    up_t, up_c = flex_ui.fmt_change(1, 1)
    dn_t, dn_c = flex_ui.fmt_change(-1, -1)
    check("漲紅跌綠", up_c == "#E53935" and dn_c == "#2E7D32" and "▲" in up_t and "▼" in dn_t)
    raw = flex_ui.plain_card("測試", "hello")
    c = FlexContainer.from_dict(raw)
    m = FlexMessage(alt_text="t", contents=c)
    check("from_dict 非空", bool(m.to_dict()["contents"].get("body")))

    print("======== 2. 記帳 ========")
    p = parse_expense_utterance("8/5 晚餐 210元")
    check("解析指定日期", bool(p and p[0] == 210 and p[1] == "晚餐"), str(p))
    p2 = parse_expense_utterance("今天早餐 100元")
    check("解析今天早餐", bool(p2 and p2[0] == 100), str(p2))
    with session_scope() as s:
        data = record_expense(s, UID, amount=77, note="自我測試便當", spent_at=p2[2] if p2 else None)
        eid = data.get("id")
        check("寫入記帳", bool(eid) and not data.get("error"), str(data))
        msg = summarize_expenses_period(s, UID, "今天")
        check("今日統計有資料", "77" in msg or "自我測試" in msg, msg[:120])
        if eid:
            del_msg = delete_expense_record(s, UID, int(eid))
            check("刪除記帳", "已刪除" in del_msg, del_msg)
    a_case("8/7 午餐 120元", "bubble")
    a_case("今天花了多少", "bubble")
    a_case("今日總花費", "bubble")
    a_case("八月消費明細", "bubble")
    a_case("本月類別圓餅圖", "bubble")
    a_case("刪除#999999這筆", "bubble")
    a_case("記得那天晚餐花了兩百一", None)

    print("======== 3. 股市 ========")
    q = get_stock_quote("2330")
    check("2330 報價", not q.get("error") and q.get("price") is not None, str(q.get("error")))
    q2 = get_stock_quote("欣興")
    check("欣興→3037", not q2.get("error") and str(q2.get("symbol")) == "3037", str(q2))
    check("format_quote", "3037" in format_quote(q2) or "欣興" in format_quote(q2))
    parts = get_stock_movers_both_data(5, "ALL")
    check("漲跌幅雙向", len(parts) == 2 and all(len(p[2]) >= 3 for p in parts), str([(p[0], len(p[2])) for p in parts]))
    ms = get_market_summary()
    check("大盤摘要", "加權" in ms or "台股" in ms or "2330" in ms, ms[:100])
    a_case("2330", "bubble")
    a_case("欣興股價", "bubble")
    a_case("漲幅前十", "bubble")  # 只問漲 → 單卡；漲跌都問才 carousel
    a_case("大盤", "bubble")
    a_case("算我盈虧", "bubble")

    print("======== 4. 持股 data ========")
    with session_scope() as s:
        data = portfolio_summary_data(s, UID)
        check("portfolio_summary_data", isinstance(data.get("rows"), list))

    print("======== 5. 行事曆 ========")
    check("OAuth 已設定", calendar_configured())
    check("OAuth 已授權", calendar_authorized())
    for phrase, key in [
        ("我這週有什麼行程", "本週"),
        ("明天行程", "2026"),
        ("本月行程", "/"),
        ("週末有什麼安排", "週末"),
    ]:
        rr = resolve_calendar_range(phrase)
        check(f"期間 {phrase}", rr is not None and key in (rr[2] if rr else ""), str(rr[2] if rr else None))
    if calendar_authorized():
        try:
            label, rows = list_events_rows_for_period("本月行程")
            check("本月行程 API", isinstance(rows, list), f"{label} n={len(rows)}")
        except Exception as exc:  # noqa: BLE001
            check("本月行程 API", False, str(exc))
        a_case("明天行程", "bubble")
        a_case("本月行程", "bubble")
        a_case("我這週有什麼行程", "bubble")

    print("======== 6. 路由互不誤傷 ========")
    from app.intent import Intent, classify_intent

    for phrase, want in [
        ("今日總花費", Intent.EXPENSE_QUERY),
        ("今天花了多少", Intent.EXPENSE_QUERY),
        ("欣興股價", Intent.STOCK_QUOTE),
        ("2330", Intent.STOCK_QUOTE),
        ("00981A 28塊 買2張", Intent.STOCK_BUY),
        ("這週行程", Intent.CALENDAR_LIST),
        ("判斷 2330", Intent.STOCK_TECH),
        ("判斷00947", Intent.STOCK_TECH),
        ("分析台積電", Intent.STOCK_TECH),
        ("分析 AAPL", Intent.STOCK_TECH),
    ]:
        got = classify_intent(phrase)
        check(f"intent {phrase}", got == want, f"{got.value}")

    r = try_route_a(UID, "八月消費明細")
    check(
        "八月消費走記帳卡",
        r is not None
        and flex_ok(r)
        and any(k in (r.text or "") for k in ("筆", "$", "合計", "消費", "記帳", "08")),
        (r.text[:80] if r else None),
    )
    r_spend = try_route_a(UID, "今日總花費")
    check(
        "今日總花費走記帳（非股價）",
        r_spend is not None
        and flex_ok(r_spend)
        and "找不到" not in (r_spend.text or "")
        and "代號" not in (r_spend.text or "")
        and any(k in (r_spend.text or "") for k in ("記帳", "花費", "合計", "期間", "沒有")),
        (r_spend.text[:80] if r_spend else None),
    )
    r2 = try_route_a(UID, "欣興股價")
    check(
        "欣興走報價卡",
        r2 is not None and flex_ok(r2) and ("3037" in (r2.text or "") or "欣興" in (r2.text or "")),
        (r2.text[:80] if r2 else None),
    )

    print("======== 7. HTTP ========")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    h = client.get("/")
    check("GET /", h.status_code == 200 and h.json().get("status") == "ok")
    try:
        import urllib.request

        urllib.request.urlopen("http://127.0.0.1:8080/", timeout=3)
        check("本機 8080 存活", True)
    except Exception as exc:  # noqa: BLE001
        check("本機 8080 存活", False, str(exc))

    print()
    print(f"結果：passed={passed} failed={failed}")
    if errors:
        print("--- failures ---")
        for e in errors:
            print(e)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
