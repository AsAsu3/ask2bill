#!/usr/bin/env python3
"""
Ask2Bill 台帳ダッシュボード生成 (デモ7)

請求台帳 (invoices.csv) を読み、AG Grid のグリッド UI を持つ1枚ページ
(dashboard.html) を生成する。標準ライブラリのみ。

グリッドは AG Grid Community (CDN) で描画する。CDN が読み込めない環境
でも同じ行データで素の表 (フォールバック) を表示するため、ページ自体は
オフラインでも台帳を確認できる。行データは生成時点の台帳のスナップ
ショットであり、台帳が変わったら本スクリプトを再実行すること。

使い方:
    python3 receiptdesk.py --demo        # 台帳を最新化
    python3 build_dashboard.py           # demo_data から dashboard.html を生成
    python3 build_dashboard.py --data-dir demo_data_sandbox --out dashboard-sandbox.html

列: 受付番号 / 受付日時 / 顧客名 / サービス / 優先度 / 見積金額 /
    請求番号 / PayPal注文ID / ステータス / 支払完了日時 / 支払いリンク
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

COLUMNS = [
    ("受付番号", "受付番号"), ("受付日時", "受付日時"),
    ("顧客名", "顧客名"), ("サービス", "サービス"),
    ("優先度", "優先度"), ("見積金額", "見積金額"),
    ("請求番号", "請求番号"), ("PayPal注文ID", "PayPal注文ID"),
    ("支払いリンク", "支払いリンク"), ("ステータス", "ステータス"),
    ("支払完了日時", "支払完了日時"),
]

AG_GRID_CDN = "https://cdn.jsdelivr.net/npm/ag-grid-community/dist/ag-grid-community.min.js"


def load_rows(data_dir: Path) -> list[dict]:
    path = data_dir / "invoices.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_html(rows: list[dict]) -> str:
    clean = [{key: (row.get(key) or "") for key, _ in COLUMNS} for row in rows]
    row_json = json.dumps(clean, ensure_ascii=False)
    col_defs = json.dumps(
        [{"field": key, "headerName": label, "filter": True, "sortable": True,
          "resizable": True}
         for key, label in COLUMNS], ensure_ascii=False)
    fallback_rows = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(row.get(key) or ''))}</td>"
            for key, _ in COLUMNS) + "</tr>"
        for row in clean)
    headers = "".join(f"<th>{html.escape(label)}</th>" for _, label in COLUMNS)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>Ask2Bill — 請求台帳ダッシュボード</title>
<script src="{AG_GRID_CDN}"></script>
<style>
  body {{ font-family: sans-serif; margin: 24px; }}
  #grid {{ height: 480px; width: 100%; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #ccc; padding: 4px 8px; font-size: 14px; }}
  .note {{ color: #555; font-size: 13px; }}
</style>
</head>
<body>
<h1>Ask2Bill — 請求台帳</h1>
<p class="note">行数: {len(clean)} 件。台帳 (invoices.csv) のスナップショット。
AG Grid が読み込めない場合は下の素の表で同じ内容を表示する。</p>
<div id="grid" class="ag-theme-alpine"></div>
<table id="fallback"><thead><tr>{headers}</tr></thead>
<tbody>{fallback_rows}</tbody></table>
<script>
const rowData = {row_json};
const columnDefs = {col_defs};
if (window.agGrid) {{
  document.getElementById('fallback').style.display = 'none';
  agGrid.createGrid(document.getElementById('grid'),
    {{ rowData: rowData, columnDefs: columnDefs, defaultColDef: {{ filter: true }} }});
}} else {{
  document.getElementById('grid').innerHTML =
    '<p class="note">AG Grid の CDN が読み込めなかったため、下の表を表示しています。</p>';
}}
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask2Bill 台帳ダッシュボード生成")
    parser.add_argument("--data-dir", default="demo_data")
    parser.add_argument("--out", default="dashboard.html")
    args = parser.parse_args()
    rows = load_rows(BASE_DIR / args.data_dir)
    out = BASE_DIR / args.out
    out.write_text(build_html(rows), encoding="utf-8")
    print(f"生成: {out} ({len(rows)} 件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
