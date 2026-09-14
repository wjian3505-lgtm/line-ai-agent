"""LINE Flex Message：淺色留白簡約風。

台股習慣：漲紅、跌綠。
"""
from __future__ import annotations

import re
from typing import Any, Optional

# 設計 token
C_BG = "#F4F5F7"
C_CARD = "#FFFFFF"
C_TITLE = "#1C1C1E"
C_MUTED = "#8E8E93"
C_LINE = "#E5E5EA"
C_UP = "#E53935"
C_DOWN = "#2E7D32"
C_FLAT = "#8E8E93"
C_ACCENT = "#3A7AFE"
C_OK = "#1B8F5A"
C_WARN = "#C62828"


def strip_markdown(text: str) -> str:
    """去掉 LINE 不會渲染的 Markdown 符號。"""
    t = text or ""
    t = t.replace("**", "")
    t = t.replace("__", "")
    t = t.replace("`", "")
    t = re.sub(r"^\s*[\-\*•]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _text(
    text: str,
    *,
    size: str = "sm",
    color: str = C_TITLE,
    weight: str = "regular",
    wrap: bool = True,
    margin: str = "none",
    flex: Optional[int] = None,
    align: Optional[str] = None,
) -> dict:
    node: dict[str, Any] = {
        "type": "text",
        "text": text[:2000] if text else " ",
        "size": size,
        "color": color,
        "weight": weight,
        "wrap": wrap,
    }
    if margin != "none":
        node["margin"] = margin
    if flex is not None:
        node["flex"] = flex
    if align:
        node["align"] = align
    return node


def _sep(margin: str = "lg") -> dict:
    return {"type": "separator", "margin": margin, "color": C_LINE}


def _kv_row(label: str, value: str, value_color: str = C_TITLE) -> dict:
    return {
        "type": "box",
        "layout": "baseline",
        "margin": "md",
        "contents": [
            _text(label, size="xs", color=C_MUTED, flex=2),
            _text(value, size="sm", color=value_color, weight="bold", flex=5, align="end"),
        ],
    }


def _header(title: str, subtitle: str = "") -> dict:
    contents = [
        _text(title, size="lg", weight="bold", color=C_TITLE),
    ]
    if subtitle:
        contents.append(_text(subtitle, size="xs", color=C_MUTED, margin="sm"))
    return {
        "type": "box",
        "layout": "vertical",
        "contents": contents,
        "paddingAll": "20px",
        "paddingBottom": "12px",
        "backgroundColor": C_CARD,
    }


def _body(contents: list, *, padding_top: str = "4px") -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "contents": contents,
        "paddingAll": "20px",
        "paddingTop": padding_top,
        "backgroundColor": C_CARD,
    }


def bubble(header_title: str, body_contents: list, *, subtitle: str = "") -> dict:
    return {
        "type": "bubble",
        "size": "mega",
        "styles": {
            "body": {"backgroundColor": C_CARD},
            "header": {"backgroundColor": C_CARD},
        },
        "header": _header(header_title, subtitle),
        "body": _body(body_contents),
    }


def carousel(bubbles: list[dict]) -> dict:
    return {"type": "carousel", "contents": bubbles[:12]}


def plain_card(title: str, text: str) -> dict:
    """通用文字卡（B 路徑／錯誤／未結構化回覆）。"""
    clean = strip_markdown(text)
    lines = [ln for ln in clean.splitlines() if ln.strip()]
    contents: list = []
    for i, ln in enumerate(lines[:40]):
        contents.append(
            _text(ln, size="sm", color=C_TITLE, margin="none" if i == 0 else "sm")
        )
    if not contents:
        contents = [_text("（無內容）", color=C_MUTED)]
    return bubble(title, contents)


def stock_pnl_text_card(title: str, text: str) -> dict:
    """文字版持股／股價回覆：含 +／賺／▲ 紅字，含 -／賠／▼ 綠字。"""
    clean = strip_markdown(text)
    lines = [ln for ln in clean.splitlines() if ln.strip()]
    contents: list = []
    for i, ln in enumerate(lines[:50]):
        color = C_TITLE
        # 優先判斷虧損（避免「+-」混淆）
        if re.search(r"(賠|▼|跌)", ln) or re.search(
            r"(\(\s*-|\-\d+(?:\.\d+)?\s*%|盈虧：\s*-)", ln
        ):
            color = C_DOWN
        elif re.search(r"(賺|▲|漲)", ln) or re.search(
            r"(\(\s*\+|\+\d+(?:\.\d+)?\s*%|盈虧：\s*\+)", ln
        ):
            color = C_UP
        contents.append(
            _text(
                ln,
                size="sm",
                color=color,
                weight="bold" if color != C_TITLE else "regular",
                margin="none" if i == 0 else "sm",
            )
        )
    if not contents:
        contents = [_text("（無內容）", color=C_MUTED)]
    return bubble(title, contents, subtitle="紅漲綠跌")


def change_color(change: Optional[float]) -> str:
    if change is None:
        return C_FLAT
    if change > 0:
        return C_UP
    if change < 0:
        return C_DOWN
    return C_FLAT


def fmt_change(change, change_percent) -> tuple[str, str]:
    """回傳 (顯示文字, 顏色)。台股：漲紅 ▲、跌綠 ▼。"""
    try:
        ch = float(change)
        pct = float(change_percent)
    except (TypeError, ValueError):
        return ("－", C_FLAT)
    if ch > 0:
        return (f"▲ {pct:+.2f}%", C_UP)
    if ch < 0:
        return (f"▼ {pct:+.2f}%", C_DOWN)
    return ("－ 0.00%", C_FLAT)


def fmt_change_detail(change, change_percent) -> tuple[str, str]:
    """含點數的漲跌文字。"""
    try:
        ch = float(change)
        pct = float(change_percent)
    except (TypeError, ValueError):
        return ("－", C_FLAT)
    if ch > 0:
        return (f"▲ {abs(ch):.2f}（{pct:+.2f}%）", C_UP)
    if ch < 0:
        return (f"▼ {abs(ch):.2f}（{pct:+.2f}%）", C_DOWN)
    return ("－ 0.00（0.00%）", C_FLAT)


def expense_added(
    *,
    expense_id: int,
    amount: float,
    category: str,
    note: str,
    when_label: str,
) -> dict:
    return bubble(
        "已記帳",
        [
            _kv_row("編號", f"#{expense_id}"),
            _kv_row("項目", note or "（無）"),
            _kv_row("金額", f"${amount:,.0f}", C_ACCENT),
            _kv_row("類別", category),
            _kv_row("日期", when_label),
        ],
        subtitle="日常花費",
    )


def expenses_added_batch(
    *,
    rows: list[dict],
    when_label: str,
) -> dict:
    """一次多筆記帳成功卡。rows: [{id, note, amount, category}, ...]"""
    total = sum(float(r.get("amount") or 0) for r in rows)
    body: list = [
        _kv_row("筆數", f"{len(rows)} 筆"),
        _kv_row("合計", f"${total:,.0f}", C_ACCENT),
        _kv_row("日期", when_label),
        {"type": "separator", "margin": "md"},
    ]
    for r in rows[:12]:
        note = r.get("note") or "（無）"
        body.append(
            _kv_row(
                f"#{r.get('id')}",
                f"{note}  ${float(r.get('amount') or 0):,.0f}（{r.get('category') or ''}）",
            )
        )
    if len(rows) > 12:
        body.append(
            {
                "type": "text",
                "text": f"…還有 {len(rows) - 12} 筆",
                "size": "xs",
                "color": C_MUTED,
                "margin": "sm",
            }
        )
    return bubble("已記帳", body, subtitle=f"一次 {len(rows)} 筆")


def holding_added(
    *,
    holding_id: int,
    symbol: str,
    name: str,
    buy_price: float,
    quantity: float,
    lots: float,
    cost: float,
) -> dict:
    return bubble(
        "已記錄持股",
        [
            _kv_row("編號", f"#{holding_id}"),
            _kv_row("標的", f"{symbol}  {name}".strip()),
            _kv_row("買價", f"{buy_price:g}"),
            _kv_row("數量", f"{quantity:.0f} 股（{lots:g} 張）"),
            _kv_row("成本", f"${cost:,.0f}", C_ACCENT),
        ],
        subtitle="Portfolio",
    )


def expense_deleted(
    *,
    expense_id: int,
    amount: float,
    category: str,
    note: str,
    when_label: str,
) -> dict:
    return bubble(
        "已刪除記帳",
        [
            _kv_row("編號", f"#{expense_id}"),
            _kv_row("項目", note or "（無）"),
            _kv_row("金額", f"${amount:,.0f}"),
            _kv_row("類別", category),
            _kv_row("日期", when_label),
        ],
        subtitle="Removed",
    )


def expense_summary(
    *,
    label: str,
    total: float,
    count: int,
    by_cat: list[tuple[str, float]],
    lines: list[str],
) -> dict:
    contents: list = [
        _text(f"合計  ${total:,.0f}", size="xl", weight="bold", color=C_TITLE),
        _text(f"{count} 筆 · {label}", size="xs", color=C_MUTED, margin="sm"),
        _sep(),
    ]
    if by_cat:
        contents.append(_text("類別", size="xs", color=C_MUTED, margin="lg"))
        for cat, amt in by_cat[:8]:
            pct = (amt / total * 100) if total else 0
            contents.append(_kv_row(cat, f"${amt:,.0f}  {pct:.0f}%"))
    if lines:
        contents.append(_sep())
        contents.append(_text("細項", size="xs", color=C_MUTED, margin="lg"))
        for ln in lines[:15]:
            contents.append(_text(ln, size="xs", color=C_TITLE, margin="sm"))
    return bubble("花費統計", contents, subtitle=label)


def expense_full_report(
    *,
    label: str,
    total: float,
    count: int,
    by_cat: list[tuple[str, float]],
    detail_lines: list[str],
) -> dict:
    """合計＋類別＋完整明細（多頁 carousel，避免被截斷）。"""
    bubbles: list[dict] = []

    # 1) 總覽卡
    overview: list = [
        _text(f"${total:,.0f}", size="3xl", weight="bold", color=C_ACCENT),
        _text(f"{label} · {count} 筆", size="sm", color=C_MUTED, margin="sm"),
        _sep("md"),
        _text("類別占比", size="xs", color=C_MUTED, margin="md"),
    ]
    if by_cat:
        for cat, amt in by_cat[:8]:
            pct = (amt / total * 100) if total else 0
            overview.append(_kv_row(cat, f"${amt:,.0f}  {pct:.1f}%"))
    else:
        overview.append(_text("（無類別資料）", color=C_MUTED))
    if detail_lines:
        overview.append(_sep("md"))
        overview.append(
            _text("往右滑看完整明細 →", size="xs", color=C_MUTED, margin="sm")
        )
    bubbles.append(bubble("花費總覽", overview, subtitle=label))

    # 2) 明細分頁（每頁 12 筆；carousel 最多 12 張，留 1 張給總覽 → 11 頁）
    per_page = 12
    max_detail_bubbles = 11
    max_items = per_page * max_detail_bubbles
    shown = detail_lines[:max_items]
    total_pages = max(1, (len(shown) + per_page - 1) // per_page) if shown else 0

    for page in range(total_pages):
        chunk = shown[page * per_page : (page + 1) * per_page]
        body: list = [
            _text(
                f"明細 {page + 1}/{total_pages}",
                size="xs",
                color=C_MUTED,
            )
        ]
        for ln in chunk:
            body.append(_text(ln, size="xs", color=C_TITLE, margin="sm"))
        bubbles.append(bubble("消費明細", body, subtitle=label))

    if len(detail_lines) > max_items:
        bubbles.append(
            bubble(
                "還有更多",
                [
                    _text(
                        f"已顯示前 {max_items} 筆，其餘 {len(detail_lines) - max_items} 筆請縮小期間再查。",
                        color=C_MUTED,
                    )
                ],
                subtitle=label,
            )
        )

    return carousel(bubbles[:12])


def calendar_events(*, label: str, rows: list[dict]) -> dict:
    """rows: {time, title, location?}"""
    if not rows:
        return bubble("行事曆", [_text("這段期間沒有行程", color=C_MUTED)], subtitle=label)
    contents: list = []
    for i, r in enumerate(rows[:20]):
        if i:
            contents.append(_sep("md"))
        contents.append(_text(r.get("time") or "", size="xs", color=C_MUTED))
        contents.append(_text(r.get("title") or "(無標題)", size="sm", weight="bold", margin="sm"))
        if r.get("location"):
            contents.append(_text(r["location"], size="xs", color=C_MUTED, margin="xs"))
    return bubble("行程", contents, subtitle=label)


def calendar_result(*, title: str, rows: list[tuple[str, str]], ok: bool = True) -> dict:
    contents = [_kv_row(k, v) for k, v in rows]
    return bubble(title, contents, subtitle="Calendar")


def stock_quote_card(q: dict) -> dict:
    if q.get("error"):
        return bubble(
            "查詢失敗",
            [_text(str(q.get("error")), color=C_WARN)],
            subtitle=str(q.get("symbol") or ""),
        )
    ch_text, ch_color = fmt_change_detail(q.get("change"), q.get("change_percent"))
    name = f"{q.get('symbol') or ''}  {q.get('name') or ''}".strip()
    price = q.get("price")
    price_s = f"{price}" if price is not None else "－"
    accent = ch_color if ch_color != C_FLAT else C_ACCENT
    card = bubble(
        name,
        [
            _text(price_s, size="3xl", weight="bold", color=ch_color),
            _text(ch_text, size="lg", weight="bold", color=ch_color, margin="sm"),
            _sep(),
            _kv_row("開盤", str(q.get("open") or "－")),
            _kv_row("最高", str(q.get("high") or "－")),
            _kv_row("最低", str(q.get("low") or "－")),
            _kv_row("成交量", str(q.get("volume") or "－")),
            _kv_row("資料日", str(q.get("date") or "－")),
        ],
        subtitle="個股報價",
    )
    card["hero"] = {
        "type": "box",
        "layout": "vertical",
        "contents": [{"type": "filler"}],
        "backgroundColor": accent,
        "height": "6px",
    }
    return card


def stock_movers_card(
    *,
    title: str,
    date_label: str,
    rows: list[dict],
    direction: str,
) -> dict:
    """漲跌排行卡：紅 ▲ / 綠 ▼，掃一眼就能分漲跌。"""
    accent = C_UP if direction == "up" else C_DOWN
    arrow = "▲" if direction == "up" else "▼"
    contents: list = [
        {
            "type": "box",
            "layout": "baseline",
            "contents": [
                _text(arrow, size="lg", color=accent, weight="bold", flex=0),
                _text(
                    "漲幅" if direction == "up" else "跌幅",
                    size="sm",
                    color=accent,
                    weight="bold",
                    margin="sm",
                    flex=2,
                ),
                _text(date_label or "最新交易日", size="xxs", color=C_MUTED, flex=5, align="end"),
            ],
        },
        _sep("md"),
    ]
    for i, r in enumerate(rows[:10], 1):
        ch_text, ch_color = fmt_change(r.get("change"), r.get("change_percent"))
        # 右側大字漲跌幅
        pct_node = _text(ch_text, size="md", color=ch_color, weight="bold", align="end", flex=3)
        name = (r.get("name") or "").strip() or "－"
        symbol = str(r.get("symbol") or "")
        market = r.get("market") or ""
        price = r.get("price")
        price_s = f"{price}" if price is not None else "－"
        contents.append(
            {
                "type": "box",
                "layout": "horizontal",
                "margin": "lg",
                "spacing": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 0,
                        "width": "28px",
                        "contents": [
                            _text(str(i), size="sm", color=C_MUTED, weight="bold", align="center"),
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 5,
                        "contents": [
                            _text(f"{symbol}  {name}", size="sm", weight="bold", color=C_TITLE),
                            _text(
                                f"{price_s}　{market}",
                                size="xs",
                                color=C_MUTED,
                                margin="xs",
                            ),
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 3,
                        "justifyContent": "center",
                        "contents": [pct_node],
                    },
                ],
            }
        )
        if i < min(10, len(rows)):
            contents.append(
                {
                    "type": "separator",
                    "margin": "md",
                    "color": "#F0F0F2",
                }
            )

    head = bubble(title, contents, subtitle="台股排行")
    head["hero"] = {
        "type": "box",
        "layout": "vertical",
        "contents": [{"type": "filler"}],
        "backgroundColor": accent,
        "height": "8px",
    }
    return head


def portfolio_card(*, rows: list[dict], total: Optional[dict] = None) -> dict:
    """持股卡：漲紅跌綠（台股習慣）。"""
    if not rows:
        return bubble(
            "持股損益",
            [_text("目前沒有持股紀錄", color=C_MUTED)],
            subtitle="Portfolio",
        )
    contents: list = []
    for i, r in enumerate(rows):
        if i:
            contents.append(_sep("md"))
        if r.get("error"):
            contents.append(
                _text(
                    f"#{r.get('id')}  {r.get('symbol')}  {r.get('name') or ''}".strip(),
                    size="sm",
                    weight="bold",
                )
            )
            contents.append(_text("報價取得失敗", size="xs", color=C_WARN, margin="xs"))
            continue

        pnl = float(r.get("pnl") or 0)
        roi = float(r.get("roi") or 0)
        buy = r.get("buy")
        current = r.get("current")
        pnl_color = change_color(pnl)
        # 現價相對成本：漲紅、跌綠
        try:
            price_delta = float(current) - float(buy) if buy is not None and current is not None else None
        except (TypeError, ValueError):
            price_delta = None
        price_color = change_color(price_delta)

        if pnl > 0:
            pnl_text = f"盈虧：+{pnl:,.0f} 元（{roi:+.2f}%）"
        elif pnl < 0:
            pnl_text = f"盈虧：{pnl:,.0f} 元（{roi:+.2f}%）"
        else:
            pnl_text = f"盈虧：0 元（{roi:+.2f}%）"

        contents.append(
            _text(
                f"{r.get('symbol')}  {r.get('name') or ''}".strip(),
                size="md",
                weight="bold",
            )
        )
        contents.append(
            _text(f"買入價：{buy}", size="xs", color=C_MUTED, margin="xs")
        )
        contents.append(
            {
                "type": "box",
                "layout": "baseline",
                "margin": "xs",
                "contents": [
                    _text("現在價：", size="xs", color=C_MUTED, flex=0),
                    _text(
                        f"{current}",
                        size="sm",
                        color=price_color,
                        weight="bold",
                        flex=0,
                    ),
                ],
            }
        )
        contents.append(
            _text(f"股數：{r.get('qty')}", size="xs", color=C_MUTED, margin="xs")
        )
        contents.append(
            _text(pnl_text, size="sm", color=pnl_color, weight="bold", margin="sm")
        )

    if total:
        contents.append(_sep())
        tpnl = float(total.get("pnl") or 0)
        troi = float(total.get("roi") or 0)
        tcolor = change_color(tpnl)
        if tpnl > 0:
            t_text = f"+{tpnl:,.0f} 元（總報酬率 {troi:+.2f}%）"
        elif tpnl < 0:
            t_text = f"{tpnl:,.0f} 元（總報酬率 {troi:+.2f}%）"
        else:
            t_text = f"0 元（總報酬率 {troi:+.2f}%）"
        contents.append(_kv_row("總成本", f"{total.get('cost', 0):,.0f} 元"))
        contents.append(_kv_row("總市值", f"{total.get('value', 0):,.0f} 元"))
        contents.append(_kv_row("總盈虧", t_text, tcolor))

    card = bubble("持股收益", contents, subtitle="Portfolio")
    # 頂部色條：整體賺錢紅、賠錢綠
    overall = (total or {}).get("pnl")
    bar = change_color(overall) if overall is not None else C_ACCENT
    card["hero"] = {
        "type": "box",
        "layout": "vertical",
        "contents": [{"type": "filler"}],
        "backgroundColor": bar,
        "height": "6px",
    }
    return card


def market_summary_card(*, index: Optional[dict], watch: list[dict]) -> dict:
    contents: list = []
    if index and not index.get("error"):
        ch_text, ch_color = fmt_change(index.get("change"), index.get("change_percent"))
        contents.append(_text("加權指數", size="xs", color=C_MUTED))
        contents.append(
            _text(str(index.get("price") or "－"), size="xxl", weight="bold", color=ch_color, margin="sm")
        )
        contents.append(_text(ch_text, size="sm", color=ch_color, weight="bold", margin="xs"))
        contents.append(_sep())
    for w in watch[:12]:
        if w.get("error"):
            contents.append(_kv_row(str(w.get("symbol")), str(w.get("error")), C_WARN))
            continue
        ch_text, ch_color = fmt_change(w.get("change"), w.get("change_percent"))
        label = f"{w.get('symbol')} {w.get('name') or ''}".strip()
        contents.append(
            {
                "type": "box",
                "layout": "baseline",
                "margin": "md",
                "contents": [
                    _text(label, size="sm", flex=4),
                    _text(
                        str(w.get("price") or "－"),
                        size="sm",
                        weight="bold",
                        color=ch_color,
                        flex=2,
                        align="end",
                    ),
                ],
            }
        )
        contents.append(_text(ch_text, size="xs", color=ch_color, weight="bold", align="end", margin="xs"))
    return bubble("今日台股", contents, subtitle="Market")


def stock_tech_card(
    *,
    symbol: str,
    name: str = "",
    verdict: str,
    action: str = "",
    matched_rules: list[str] | None = None,
    failed_rules: list[str] | None = None,
    risk_note: str = "",
    confidence: str = "中",
    snapshot: dict | None = None,
    disclaimer: str = "本判定僅依據預設技術規則，非投資建議",
) -> dict:
    """技術面判定卡：建議買入／賣出／觀望。"""
    title = f"{symbol} {name}".strip()
    label = action or (
        "建議買入" if verdict == "符合進場"
        else "建議賣出" if verdict == "符合出場"
        else "建議觀望"
    )
    if "買入" in label:
        v_color = C_OK
    elif "賣出" in label:
        v_color = C_WARN
    else:
        v_color = C_ACCENT
    matched = "、".join(matched_rules or []) or "無"
    failed = "、".join(failed_rules or []) or "無"
    snap = snapshot or {}
    contents = [
        _text(label, size="xl", weight="bold", color=v_color),
        _text(str(verdict), size="xs", color=C_MUTED, margin="xs"),
    ]
    if snap:
        contents.append(
            _text(
                f"收盤 {snap.get('close')}｜中軌 {snap.get('bb_middle')}｜"
                f"RSI {snap.get('rsi')}｜MACD柱 {snap.get('macd_hist')}",
                size="xs",
                color=C_MUTED,
                margin="sm",
            )
        )
    contents.extend(
        [
            _sep("lg"),
            _text(f"命中：{matched}", size="sm", color=C_OK, margin="md"),
            _text(f"未中：{failed}", size="sm", color=C_WARN, margin="sm"),
            _text(f"風險：{risk_note or '—'}", size="sm", color=C_TITLE, margin="md"),
            _text(f"信心度：{confidence or '中'}", size="sm", color=C_MUTED, margin="sm"),
            _sep("lg"),
            _text(disclaimer, size="xs", color=C_MUTED, margin="md"),
        ]
    )
    return bubble(title, contents, subtitle="技術面買賣建議")
