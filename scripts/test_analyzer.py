"""本機測試技術面判定全流程（不經 LINE）。

用法（專案根目錄）：
    .venv\\Scripts\\python.exe scripts\\test_analyzer.py
    .venv\\Scripts\\python.exe scripts\\test_analyzer.py 2330
    .venv\\Scripts\\python.exe scripts\\test_analyzer.py AAPL
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analyzer import run_analysis  # noqa: E402
from app.intent import Intent, classify_intent, extract_tech_query  # noqa: E402


def main() -> int:
    target = (sys.argv[1] if len(sys.argv) > 1 else "2330").strip()
    phrase = f"判斷 {target}"
    intent = classify_intent(phrase)
    query = extract_tech_query(phrase)
    print(f"句子：{phrase}")
    print(f"intent：{intent.value}（期望 {Intent.STOCK_TECH.value}）")
    print(f"抽出標的：{query}")
    if intent != Intent.STOCK_TECH or not query:
        print("意圖分流失敗")
        return 1

    result = run_analysis(query)
    if not result.get("ok"):
        print("失敗：", result.get("error"))
        return 1

    payload = result["payload"]
    print(f"標的：{result.get('symbol')} {result.get('name')}")
    print(f"市場：{result.get('market')}／指標來源：{result.get('indicator_source')}")
    print("--- 回覆字串 ---")
    print(result.get("alt_text"))
    print("--- JSON ---")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
