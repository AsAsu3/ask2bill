#!/usr/bin/env python3
"""
Ask2Bill (デモ7): 受付 → 分類 → 見積 → オーナー承認 → 請求 (モック) → 入金確認

PayPal AI Hackathon (締切 2026-11-13 05:00 JST) への提出プロトタイプ。
土台は Rank 2 の demo-bot (FAQ/受付) と demo2-pipeline (受付→分類→台帳→通知)。
新規部分は「見積ドラフト → オーナー承認ゲート → 請求レイヤー」だけ。

安全設計 (提出物の中核アピール):
  - AI は見積「ドラフト」までしか作らない。金額の確定と請求発行はオーナー承認が必須
    (owner_decision = approve が無い限り、請求書は絶対に発行されない)。
  - 決済は paypal_adapter の interface 経由のみ。本体は実装を知らない。
    検証用 MockPayPalAdapter (--demo の既定) と実 Sandbox 用 SandboxPayPalAdapter
    (Orders/Invoicing API v2、認証情報は環境変数、現在は未検証) が差し替え可能。
    切替は環境変数 RECEIPTDESK_PAYPAL_ADAPTER=mock|sandbox。
    ★モックは検証専用であり提出物ではない (要件: 実 Sandbox が中核)★
  - 請求の発行方式は環境変数 RECEIPTDESK_ISSUE_MODE で選ぶ (既定 invoice)。
    orders を指定すると、請求書 (Invoicing API) の代わりにチェックアウト注文
    (Orders API v2) を発行して承認リンクを顧客へ渡す。Sandbox で Invoicing API
    が 403 NOT_AUTHORIZED になる場合 (2026-10-30 の実スモークで確認) の
    迂回路であり、承認ゲート・台帳・リマインドの流れは invoice 方式と同じ。
    提出文の「invoice / checkout order」の記述はこの両方式に対応する。
  - 台帳 (invoices.csv) が唯一の真実。ステータス: DRAFT_ESTIMATE / AWAITING_OWNER /
    REJECTED_BY_OWNER / UNPAID / PAID / REMINDED。

使い方:
    python3 receiptdesk.py --demo     # 3シナリオ一気通貫 (承認→入金 / 差し戻し / 未払い)
    echo '{"channel":"webform","name":"山田","contact":"a@example.com",
          "body":"ウェブサイト制作の見積がほしい","owner_decision":"approve",
          "simulate_payment":true}' | python3 receiptdesk.py

依存は標準ライブラリのみ。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from paypal_adapter import get_adapter

JST = timezone(timedelta(hours=9))

LEDGER_SCHEMA = [
    "受付番号", "受付日時", "顧客名", "連絡先", "サービス", "優先度",
    "見積金額", "請求番号", "PayPal注文ID", "支払いリンク",
    "ステータス", "支払完了日時",
]


def now_jst() -> datetime:
    return datetime.now(JST)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def classify(body: str, catalog: dict) -> tuple[str, int, str, str]:
    """本文 → (サービス名, 見積金額JPY, 優先度, 判定メモ)。demo2 の分類思想を継承。"""
    service, price, hit = "その他", 0, None
    for svc in catalog.get("services", []):
        for kw in svc.get("keywords", []):
            if kw and kw in body:
                service, price, hit = svc["name"], int(svc["base_price_jpy"]), kw
                break
        if hit:
            break
    priority, pri_kw = "通常", None
    for kw in catalog.get("priority_keywords", []):
        if kw and kw in body:
            priority, pri_kw = "高", kw
            break
    notes = []
    if hit:
        notes.append(f"サービス判定キーワード:{hit}")
    if pri_kw:
        notes.append(f"優先度判定キーワード:{pri_kw}")
    return service, price, priority, " / ".join(notes)


class Ledger:
    """請求台帳 (CSV)。demo2 の CsvLocalLedger と同じ「台帳が唯一の真実」の考え方。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=LEDGER_SCHEMA).writeheader()

    def append(self, row: dict) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LEDGER_SCHEMA).writerow(
                {k: row.get(k, "") for k in LEDGER_SCHEMA})

    def update(self, ticket: str, **fields: str) -> None:
        rows = self.rows()
        for r in rows:
            if r["受付番号"] == ticket:
                r.update(fields)
        with self.path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER_SCHEMA)
            w.writeheader()
            w.writerows({k: r.get(k, "") for k in LEDGER_SCHEMA} for r in rows)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def find(self, ticket: str) -> dict | None:
        for r in self.rows():
            if r["受付番号"] == ticket:
                return r
        return None


class Ask2Bill:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = load_json(BASE_DIR / "catalog.json")
        self.paypal = get_adapter()  # 既定 mock / 環境変数で sandbox に切替
        self.templates = load_json(BASE_DIR / "templates.json")
        if not self.paypal.is_mock:
            # テンプレート文面はモック検証用に「モック」「デモのため実支払いなし」
            # と書かれている。実 Sandbox でそのまま使うと虚偽の表示になるため、
            # sandbox 選択時のみ Sandbox テスト用の文言に置き換える。
            # モック経路の出力は一切変えない。
            replacements = {
                " (モック)": " (Sandbox テスト)",
                "モック請求書": "請求書",
                "※デモのため実支払いは発生しません。":
                    "※Sandbox テストのため実資金の移動はありません。",
            }
            fixed = {}
            for key, text in self.templates.items():
                for old, new in replacements.items():
                    text = text.replace(old, new)
                fixed[key] = text
            self.templates = fixed
        self.ledger = Ledger(self.data_dir / "invoices.csv")
        self.seq_file = self.data_dir / "sequence.json"
        self._seq: dict[str, int] = {}
        if self.seq_file.exists():
            try:
                self._seq = json.loads(self.seq_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._seq = {}

    def _issue_ticket(self, now: datetime) -> str:
        day = now.strftime("%Y%m%d")
        seq = int(self._seq.get(day, 0)) + 1
        self._seq[day] = seq
        self.seq_file.write_text(json.dumps(self._seq), encoding="utf-8")
        return f"INQ-{day}-{seq:04d}"

    # -- メインフロー ----------------------------------------------------------
    def process(self, inquiry: dict) -> dict:
        now = now_jst()
        name = str(inquiry.get("name") or "").strip()
        contact = str(inquiry.get("contact") or "").strip()
        body = str(inquiry.get("body") or "").strip()
        decision = str(inquiry.get("owner_decision") or "pending").strip()
        simulate_payment = bool(inquiry.get("simulate_payment"))

        print(f"\n=== 受信 [{inquiry.get('channel', 'webform')}] "
              f"{name or '(名無し)'} <{contact or '連絡先なし'}> ===")

        # 1) 検証: 本文・連絡先が必須
        if not body or not contact:
            print("--- 問い合わせ者への返信 (不足情報) ---")
            print(self.templates["missing_info"])
            return {"status": "missing_info", "ticket": None}

        # 2) 受付番号の採番
        ticket = self._issue_ticket(now)

        # 3) 分類 → 見積ドラフト (AI の仕事はここまで。金額は未確定)
        service, amount, priority, notes = classify(body, self.catalog)
        print(f"[分類] サービス={service} / 優先度={priority}"
              + (f" ({notes})" if notes else ""))
        print(f"[見積ドラフト] ¥{amount:,} — {self.catalog['estimate_note']}")

        row = {
            "受付番号": ticket, "受付日時": now.isoformat(timespec="seconds"),
            "顧客名": name, "連絡先": contact, "サービス": service,
            "優先度": priority, "見積金額": amount, "請求番号": "",
            "PayPal注文ID": "", "支払いリンク": "",
            "ステータス": "DRAFT_ESTIMATE", "支払完了日時": "",
        }
        self.ledger.append(row)

        ctx = {"ticket": ticket, "name": name or "(名無し)", "service": service,
               "amount": f"{amount:,}", "body_head": body[:80]}

        # 4) オーナー承認ゲート: approve 以外では請求書を発行しない
        if decision == "reject":
            self.ledger.update(ticket, ステータス="REJECTED_BY_OWNER")
            print("--- 内部記録 ---")
            print(self.templates["owner_rejected_note"].format(**ctx))
            return {"status": "rejected_by_owner", "ticket": ticket,
                    "service": service, "amount": amount}
        if decision != "approve":
            self.ledger.update(ticket, ステータス="AWAITING_OWNER")
            print("--- オーナーへの通知 ---")
            print(self.templates["owner_approval_request"].format(**ctx))
            print("[ゲート] オーナー承認待ちのため、請求書は発行していません。")
            return {"status": "awaiting_owner", "ticket": ticket,
                    "service": service, "amount": amount}

        print("[ゲート] オーナー承認済み → 請求書を発行します")

        # 5) 請求発行: invoice 方式 (既定) か orders 方式 (迂回路) か。
        #    orders 方式では注文 ID を請求番号欄・PayPal注文ID欄の両方に記録し、
        #    承認リンクを支払いリンクとして台帳に残す。金額・承認ゲート・
        #    その後の UNPAID→PAID/REMINDED の流れは invoice 方式と同じ。
        issue_mode = os.environ.get(
            "RECEIPTDESK_ISSUE_MODE", "invoice").strip().lower()
        if issue_mode not in ("invoice", "orders"):
            print(f"[注意] 未知の RECEIPTDESK_ISSUE_MODE={issue_mode!r} のため "
                  "invoice 方式で続行します。")
            issue_mode = "invoice"
        if issue_mode == "orders":
            order = self.paypal.create_order(amount, reference_id=ticket)
            approve_link = ""
            for link in order.get("links", []) or []:
                if link.get("rel") in ("approve", "payer-action"):
                    approve_link = link.get("href", "")
                    break
            issue_id = order["id"]
            self.ledger.update(
                ticket, 請求番号=issue_id, PayPal注文ID=issue_id,
                支払いリンク=approve_link, ステータス="UNPAID")
        else:
            svc_entry = next((s for s in self.catalog["services"]
                              if s["name"] == service), None)
            unit_label = svc_entry["unit_label"] if svc_entry else service
            invoice = self.paypal.create_invoice(
                invoicer_name=self.catalog["invoicer_name"],
                customer_name=name, customer_contact=contact,
                items=[{"name": unit_label, "quantity": "1",
                        "unit_amount_jpy": amount}],
                note=f"受付 {ticket} の見積承認後の請求",
            )
            self.paypal.send_invoice(invoice["id"])
            invoice = self.paypal.get_invoice(invoice["id"])
            issue_id = invoice["id"]
            self.ledger.update(
                ticket, 請求番号=issue_id,
                支払いリンク=invoice["payment_link"],
                ステータス="UNPAID")
            approve_link = invoice["payment_link"]
        ctx.update({"invoice_id": issue_id, "payment_link": approve_link})
        print("--- 顧客への請求案内 ---")
        print(self.templates["invoice_to_customer"].format(**ctx))

        # 6) 入金確認: モックでは模擬 Webhook / capture で PAID に遷移する。
        #    Sandbox では支払者本人＋Webhook (invoice 方式) または承認＋capture
        #    (orders 方式) が確定経路のため、ここでは送金操作をしない。
        if simulate_payment and not self.paypal.is_mock:
            print("[注意] Sandbox アダプタでは支払いシミュレーションは行いません。"
                  "支払いは顧客がリンクで完了し、Webhook / capture で台帳に反映されます。")
        if simulate_payment and self.paypal.is_mock:
            if issue_mode == "orders":
                self.paypal.capture_order(issue_id)
                paid_order_id = issue_id
            else:
                paid = self.paypal.settle_invoice(issue_id)
                paid_order_id = paid["payment_detail"]["order_id"]
            self.ledger.update(
                ticket, ステータス="PAID",
                PayPal注文ID=paid_order_id,
                支払完了日時=now_jst().isoformat(timespec="seconds"))
            print("--- オーナーへの通知 ---")
            print(self.templates["owner_paid_notification"].format(**ctx))
            print("--- 顧客への領収連絡 ---")
            print(self.templates["paid_receipt"].format(**ctx))
            return {"status": "paid", "ticket": ticket,
                    "invoice_id": issue_id, "service": service,
                    "amount": amount}

        # 7) 未払い: リマインド文を生成して台帳を REMINDED に
        print("--- 顧客へのリマインド (未払い) ---")
        print(self.templates["unpaid_reminder"].format(**ctx))
        self.ledger.update(ticket, ステータス="REMINDED")
        return {"status": "unpaid_reminded", "ticket": ticket,
                "invoice_id": issue_id, "service": service,
                "amount": amount}


# ---------------------------------------------------------------------------
# デモ
# ---------------------------------------------------------------------------
DEMO_INQUIRIES = [
    {   # シナリオ1: 承認 → 請求 → 入金 (一気通貫の成功系)
        "channel": "webform", "name": "山田 花子",
        "contact": "yamada@example.com",
        "body": "小さな雑貨店をやっています。ウェブサイト制作の見積をお願いします。",
        "owner_decision": "approve", "simulate_payment": True,
    },
    {   # シナリオ2: 見積をオーナーが差し戻し → 請求書は発行されない
        "channel": "telegram", "name": "佐藤 健",
        "contact": "sato@example.com",
        "body": "保守プランの内容と月額を教えてください。",
        "owner_decision": "reject", "simulate_payment": False,
    },
    {   # シナリオ3: 承認 → 請求 → 未払い → リマインド
        "channel": "webform", "name": "鈴木 美咲",
        "contact": "suzuki@example.com",
        "body": "売上データの分析レポートを作ってほしいです。至急お願いします。",
        "owner_decision": "approve", "simulate_payment": False,
    },
]


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def run_demo() -> int:
    data_dir = BASE_DIR / "demo_data"
    _reset_dir(data_dir)
    desk = Ask2Bill(data_dir)

    print("=" * 64)
    print("デモ7 Ask2Bill: 受付 → 見積 → 承認 → 請求 → 入金確認")
    print(f"※ PayPal アダプタ: {desk.paypal.name}"
          + (" (検証用モック。実送金・実APIコールは行いません)"
             if desk.paypal.is_mock else " (実 Sandbox)"))
    print("=" * 64)

    results = [desk.process(inq) for inq in DEMO_INQUIRIES]

    print("\n" + "=" * 64)
    print(f"請求台帳 ({data_dir / 'invoices.csv'}):")
    print("=" * 64)
    for row in desk.ledger.rows():
        print(f"  {row['受付番号']} | {row['サービス']} | 見積 ¥{int(row['見積金額']):,} "
              f"| 請求 {row['請求番号'] or '-'} | {row['ステータス']} | {row['顧客名']}")

    print("\n" + "=" * 64)
    print("自己検証 (--demo)")
    print("=" * 64)
    rows = desk.ledger.rows()
    by_ticket = {r["受付番号"]: r for r in rows}
    r_paid = by_ticket[results[0]["ticket"]]
    r_rej = by_ticket[results[1]["ticket"]]
    r_unpaid = by_ticket[results[2]["ticket"]]
    # --demo は既定で invoice 方式。orders 方式で実行した場合は、支払いイベントの
    # 期待形が capture のみになる (請求書イベントは発生しない)。
    demo_issue_mode = os.environ.get(
        "RECEIPTDESK_ISSUE_MODE", "invoice").strip().lower()
    # orders 方式では請求書イベントが発生しないため、期待するイベント表示を変える。
    event_label = ("モック Webhook イベントが記録された (PAYMENT.CAPTURE"
                   + (" / INVOICE.PAID)" if demo_issue_mode != "orders"
                      else ")"))

    checks = [
        ("シナリオ1: 承認→請求→入金で PAID になった",
         results[0]["status"] == "paid" and r_paid["ステータス"] == "PAID"),
        ("シナリオ1: モック注文IDと支払完了日時が台帳に記録された",
         r_paid["PayPal注文ID"].startswith("MOCK-ORDER-")
         and bool(r_paid["支払完了日時"])),
        ("シナリオ1: 請求金額がカタログ価格 (¥150,000) と一致",
         int(r_paid["見積金額"]) == 150000),
        ("シナリオ2: 差し戻しでは請求書が発行されていない",
         results[1]["status"] == "rejected_by_owner"
         and r_rej["ステータス"] == "REJECTED_BY_OWNER"
         and r_rej["請求番号"] == ""),
        ("シナリオ3: 未払いは REMINDED で、支払いリンクが台帳に残る",
         results[2]["status"] == "unpaid_reminded"
         and r_unpaid["ステータス"] == "REMINDED"
         and "MOCK" in r_unpaid["支払いリンク"]),
        ("シナリオ3: 「至急」で優先度『高』に分類された",
         r_unpaid["優先度"] == "高"),
        ("台帳は3件のみ (重複・余分な請求なし)",
         len(rows) == 3),
        (event_label,
         any("CAPTURE" in e["event_type"] for e in desk.paypal.events)
         and (demo_issue_mode == "orders"
              or any("INVOICE.PAID" in e["event_type"]
                     for e in desk.paypal.events))),
    ]
    all_ok = True
    for label, ok in checks:
        print(f"  [{'OK' if ok else 'NG'}] {label}")
        all_ok = all_ok and ok
    print(f"\n総合: {'ALL OK' if all_ok else '一部 NG (ログを確認)'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask2Bill (デモ7)")
    parser.add_argument("--demo", action="store_true", help="3シナリオを一気通貫で実演")
    parser.add_argument("--data-dir", default="data", help="実行データの置き場所")
    args = parser.parse_args()

    if args.demo:
        return run_demo()

    desk = Ask2Bill(BASE_DIR / args.data_dir)
    lines = [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
    if not lines:
        print("標準入力から JSON を1行1件で入力してください (例は README.md)。")
        return 0
    results = [desk.process(json.loads(ln)) for ln in lines]
    print(f"\n処理件数: {len(results)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
