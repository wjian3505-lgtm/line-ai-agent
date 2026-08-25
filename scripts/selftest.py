"""本機自我測試（不依賴 LINE webhook）。"""
from __future__ import annotations

import traceback

from linebot.v3.messaging import FlexMessage

from app import flex_ui
from app.agent import AgentReply, _should_defer_expense_to_b, try_route_a
from app.calendar_google import calendar_authorized, list_events_rows_for_period, resolve_calendar_range
from app.db import init_db
from app.expenses import parse_expense_utterance
from app.main import _build_line_messages, app
from app.stock import get_stock_movers_both_data, get_stock_quote

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"OK  {name}")
    else:
        failed += 1
        errors.append(f"{name}: {detail}")
        print(f"FAIL {name} :: {detail}")


def main() -> int:
    init_db()
    uid = "selftest-user"

    print("=== 1) Flex / strip ===")
    stripped = flex_ui.strip_markdown("* **金額** : $90")
    check("strip markdown", "金額" in stripped and "$90" in stripped and "**" not in stripped, stripped)
    up_t, up_c = flex_ui.fmt_change(7.8, 10.0)
    dn_t, dn_c = flex_ui.fmt_change(-3.25, -9.97)
    check("漲紅", up_c == "#E53935" and "▲" in up_t, f"{up_t} {up_c}")
    check("跌綠", dn_c == "#2E7D32" and "▼" in dn_t, f"{dn_t} {dn_c}")
    card = flex_ui.expense_added(
        expense_id=1, amount=100, category="伙食", note="早餐", when_label="2026/08/07"
    )
    check("expense card type", card.get("type") == "bubble")
    FlexMessage(alt_text="t", contents=card)
    check("FlexMessage expense ok", True)

    print("=== 2) 記帳解析 ===")
    p = parse_expense_utterance("8/5 晚餐 210元")
    check("日期記帳金額", bool(p and p[0] == 210.0 and p[1] == "晚餐"), str(p))
    p2 = parse_expense_utterance("今天早餐 95元")
    check("今天早餐", bool(p2 and p2[0] == 95.0 and p2[1] == "早餐"), str(p2))
    check("口語 defer B", _should_defer_expense_to_b("記得那天晚餐花了兩百一") is True)

    print("=== 3) A 路徑 + Flex ===")
    cases = [
        ("8/7 早餐 88元", "bubble", "記帳"),
        ("今天花了多少", "bubble", "統計"),
        ("刪除#999這筆", "bubble", "刪除"),
        ("2330", "bubble", "股價"),
        ("漲幅前十", "carousel", "排行"),
        ("明天行程", "bubble", "行程"),
    ]
    for text, expect_type, label in cases:
        try:
            r = try_route_a(uid, text)
            ok = r is not None and r.flex is not None and r.flex.get("type") == expect_type
            if ok and r is not None:
                FlexMessage(alt_text=(r.text or "x")[:100], contents=r.flex)
            detail = (
                f"type={(None if not r else (r.flex or {}).get('type'))} "
                f"text={(r.text[:50] if r else None)!r}"
            )
            check(f"A {label}: {text}", ok, detail)
        except Exception:  # noqa: BLE001
            check(f"A {label}: {text}", False, traceback.format_exc(limit=2))

    print("=== 4) main 組訊息 ===")
    r = try_route_a(uid, "2330")
    assert r is not None
    msgs = _build_line_messages(r)
    check("build FlexMessage", len(msgs) >= 1 and msgs[0].__class__.__name__ == "FlexMessage")
    r2 = AgentReply(text="hello **world**", flex=None)
    msgs2 = _build_line_messages(r2)
    check("fallback plain flex", msgs2[0].__class__.__name__ == "FlexMessage")

    print("=== 5) 股市資料 ===")
    q = get_stock_quote("2330")
    check("台積電報價", not q.get("error") and q.get("price") is not None, str(q.get("error")))
    parts = get_stock_movers_both_data(5, "ALL")
    check(
        "漲跌幅資料",
        len(parts) == 2 and all(len(p[2]) > 0 for p in parts),
        str([(p[0], len(p[2])) for p in parts]),
    )

    print("=== 6) 行事曆期間 ===")
    rr = resolve_calendar_range("我這週有什麼行程")
    check("這週解析", rr is not None and "本週" in rr[2], str(rr[2] if rr else None))
    rr2 = resolve_calendar_range("本月行程")
    check("本月解析", rr2 is not None and "/" in rr2[2], str(rr2[2] if rr2 else None))
    auth = calendar_authorized()
    print(f"INFO Calendar authorized={auth}")
    if auth:
        try:
            label, rows = list_events_rows_for_period("本月行程")
            check("本月行程 rows", isinstance(rows, list), f"{label} n={len(rows)}")
            rcal = try_route_a(uid, "本月行程")
            check("本月行程 Flex", bool(rcal and rcal.flex and rcal.flex.get("type") == "bubble"))
        except Exception as exc:  # noqa: BLE001
            check("本月行程 API", False, str(exc))

    print("=== 7) B defer ===")
    r = try_route_a(uid, "記得八五那天晚餐花了兩百一")
    check("口語記帳不走 A", r is None)

    print("=== 8) health ===")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    h = client.get("/")
    check("GET /", h.status_code == 200 and h.json().get("status") == "ok", h.text[:200])

    print()
    print(f"結果：passed={passed} failed={failed}")
    if errors:
        print("--- failures ---")
        for e in errors:
            print(e)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
