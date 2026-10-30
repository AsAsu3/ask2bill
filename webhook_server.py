#!/usr/bin/env python3
"""
Ask2Bill Webhook 受信エンドポイント (デモ7)

PayPal から送られる支払い完了イベントを受け取り、請求台帳
(invoices.csv) を PAID に更新する。標準ライブラリのみ。

対応イベント:
  - PAYMENT.CAPTURE.COMPLETED (Orders 方式: 注文 capture の完了)
  - INVOICE.PAID (Invoicing 方式: 請求書の支払い完了)

冪等性: 処理済みイベント ID を data_dir/webhook_events.json に記録し、
同じイベント ID の再送は台帳を変更せずに 200 で返す。台帳が既に PAID の
行に対する再送も変更しない。対応する台帳行が見つからないイベントは
200 (ignored) を返すがイベント ID は記録しない — 台帳側が後から整った
場合にペイロードを再送して反映できるようにするため。

一致の取り方 (台帳の「請求番号」または「PayPal注文ID」と照合):
  - CAPTURE: resource.supplementary_data.related_ids.order_id
  - INVOICE.PAID: resource.id (請求書 ID) / resource.invoice.id
  台帳のステータスが UNPAID / REMINDED の行だけを PAID に更新する。

★重要 — 署名検証について★
この実装は PayPal の Webhook 署名検証 (verify-webhook-signature API)
を行わない。ローカル検証・デモ用途であり、このまま公開 URL として
公開してはいけない。公開デプロイ時は、署名検証 (webhook_id 照合を含む)
と HTTPS を追加し、検証に失敗したペイロードは 401 で拒否すること。
検証なしで公開すれば、第三者が偽の PAID イベントを送り込める。

使い方:
    python3 webhook_server.py --data-dir demo_data --port 8000
    python3 webhook_server.py --selftest   # ローカル検証 (ネットワーク外部なし)

ペイロード例 (PayPal の実イベント形式に準拠した最小形):
    {"id": "WH-...", "event_type": "INVOICE.PAID",
     "resource": {"id": "INV2-...", "status": "PAID"},
     "create_time": "2026-10-30T00:00:00Z"}
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from receiptdesk import Ledger  # noqa: E402

JST = timezone(timedelta(hours=9))
SUPPORTED_EVENTS = ("PAYMENT.CAPTURE.COMPLETED", "INVOICE.PAID")
UPDATABLE_STATUSES = ("UNPAID", "REMINDED")


def _dig(mapping: object, *keys: str) -> object:
    cur = mapping
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_match_ids(payload: dict) -> tuple[str | None, str | None]:
    """(注文ID候補, 請求書ID候補) を返す。見つからなければ None。"""
    resource = payload.get("resource") or {}
    if not isinstance(resource, dict):
        return None, None
    event_type = payload.get("event_type", "")
    order_id = None
    invoice_id = None
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        candidate = _dig(resource, "supplementary_data", "related_ids", "order_id")
        if isinstance(candidate, str) and candidate:
            order_id = candidate
        custom = resource.get("custom_id")
        if isinstance(custom, str) and custom:
            # custom_id には受付番号が入る場合があるため、台帳照合の
            # フォールバックとしてのみ使う (注文IDとしては扱わない)。
            pass
    if event_type == "INVOICE.PAID":
        candidate = resource.get("id")
        if isinstance(candidate, str) and candidate:
            invoice_id = candidate
        nested = _dig(resource, "invoice", "id")
        if isinstance(nested, str) and nested:
            invoice_id = nested
    # Invoicing の支払いでも CAPTURE イベントが届く場合に備え、
    # resource.invoice_id があれば請求書候補として拾う。
    alt = resource.get("invoice_id")
    if isinstance(alt, str) and alt:
        invoice_id = invoice_id or alt
    return order_id, invoice_id


def process_event(data_dir: str | Path, payload: dict) -> dict:
    """1件の Webhook ペイロードを処理し、結果 dict を返す。"""
    data_dir = Path(data_dir)
    event_id = payload.get("id")
    event_type = payload.get("event_type", "")
    if event_type not in SUPPORTED_EVENTS:
        return {"outcome": "ignored_unknown_event", "event_type": event_type}
    if not isinstance(event_id, str) or not event_id:
        return {"outcome": "rejected_no_event_id", "event_type": event_type}

    events_file = data_dir / "webhook_events.json"
    seen: list[str] = []
    if events_file.exists():
        try:
            seen = json.loads(events_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            seen = []
    if event_id in seen:
        return {"outcome": "ignored_duplicate", "event_id": event_id}

    order_id, invoice_id = extract_match_ids(payload)
    ledger = Ledger(data_dir / "invoices.csv")
    rows = ledger.rows()
    target = None
    for row in rows:
        if invoice_id and row.get("請求番号") == invoice_id:
            target = row
            break
        if order_id and (row.get("PayPal注文ID") == order_id
                         or row.get("請求番号") == order_id):
            target = row
            break
    if target is None:
        return {"outcome": "ignored_no_match", "event_id": event_id,
                "order_id": order_id, "invoice_id": invoice_id}
    if target.get("ステータス") not in UPDATABLE_STATUSES:
        # 既に PAID 等の行: 変更はしないが、再送時の冪等性のため記録する。
        seen.append(event_id)
        events_file.write_text(json.dumps(seen), encoding="utf-8")
        return {"outcome": "ignored_already_final",
                "ticket": target.get("受付番号"),
                "status": target.get("ステータス")}
    paid_at = payload.get("create_time")
    if not isinstance(paid_at, str) or not paid_at:
        paid_at = datetime.now(JST).isoformat(timespec="seconds")
    updates: dict[str, str] = {"ステータス": "PAID", "支払完了日時": paid_at}
    if order_id and not target.get("PayPal注文ID"):
        updates["PayPal注文ID"] = order_id
    ledger.update(target["受付番号"], **updates)
    seen.append(event_id)
    events_file.write_text(json.dumps(seen), encoding="utf-8")
    return {"outcome": "paid", "ticket": target.get("受付番号"),
            "event_id": event_id, "paid_at": paid_at}


def make_handler(data_dir: Path):
    class WebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # ログを静かにする
            pass

        def do_POST(self):  # noqa: N802
            if self.path != "/webhook/paypal":
                self._reply(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._reply(400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._reply(400, {"error": "payload_must_be_object"})
                return
            result = process_event(data_dir, payload)
            self._reply(200, result)

        def _reply(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return WebhookHandler


def run_server(data_dir: Path, port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(data_dir))
    print(f"Ask2Bill webhook listening on 127.0.0.1:{port} "
          f"(POST /webhook/paypal, data_dir={data_dir})")
    print("注意: 署名検証なしのローカル用途。公開する場合は検証と HTTPS を追加すること。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def run_selftest() -> int:
    """ローカル検証: インメモリではなく実ファイル＋実 HTTP で全分岐を確認。"""
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ask2bill-webhook-"))
    try:
        ledger_path = tmp / "invoices.csv"
        ledger = Ledger(ledger_path)
        ledger.append({
            "受付番号": "INQ-20261030-0001", "受付日時": "2026-10-30T00:00:00+09:00",
            "顧客名": "山田 花子", "連絡先": "yamada@example.com",
            "サービス": "ウェブサイト制作", "優先度": "通常", "見積金額": "150000",
            "請求番号": "INV2-TEST-0001", "PayPal注文ID": "",
            "支払いリンク": "https://www.sandbox.paypal.com/invoice/p/#INV2-TEST-0001",
            "ステータス": "UNPAID", "支払完了日時": "",
        })
        ledger.append({
            "受付番号": "INQ-20261030-0002", "受付日時": "2026-10-30T00:00:00+09:00",
            "顧客名": "佐藤 健", "連絡先": "sato@example.com",
            "サービス": "保守プラン", "優先度": "通常", "見積金額": "30000",
            "請求番号": "20V19863D4727870H", "PayPal注文ID": "20V19863D4727870H",
            "支払いリンク": "", "ステータス": "REMINDED", "支払完了日時": "",
        })

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}/webhook/paypal"

        def post(obj, raw: bytes | None = None):
            data = raw if raw is not None else json.dumps(obj).encode()
            req = Request(base, data=data, method="POST",
                          headers={"Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        checks: list[tuple[str, bool]] = []
        # 1) INVOICE.PAID で請求書行が PAID になる
        code, res = post({"id": "WH-INV-1", "event_type": "INVOICE.PAID",
                          "create_time": "2026-10-30T01:00:00Z",
                          "resource": {"id": "INV2-TEST-0001", "status": "PAID"}})
        row1 = Ledger(ledger_path).find("INQ-20261030-0001")
        checks.append(("INVOICE.PAID で UNPAID→PAID",
                       code == 200 and res.get("outcome") == "paid"
                       and row1["ステータス"] == "PAID"
                       and row1["支払完了日時"] == "2026-10-30T01:00:00Z"))
        # 2) 同じイベント ID の再送は冪等 (変更なし)
        code, res = post({"id": "WH-INV-1", "event_type": "INVOICE.PAID",
                          "create_time": "2026-10-30T02:00:00Z",
                          "resource": {"id": "INV2-TEST-0001", "status": "PAID"}})
        row1 = Ledger(ledger_path).find("INQ-20261030-0001")
        checks.append(("同一イベント ID の再送は無視され支払日時が変わらない",
                       code == 200 and res.get("outcome") == "ignored_duplicate"
                       and row1["支払完了日時"] == "2026-10-30T01:00:00Z"))
        # 3) CAPTURE で注文行が PAID になる
        code, res = post({"id": "WH-CAP-1",
                          "event_type": "PAYMENT.CAPTURE.COMPLETED",
                          "create_time": "2026-10-30T01:05:00Z",
                          "resource": {"id": "CAP-TEST-1",
                                       "supplementary_data": {"related_ids": {
                                           "order_id": "20V19863D4727870H"}}}})
        row2 = Ledger(ledger_path).find("INQ-20261030-0002")
        checks.append(("CAPTURE で REMINDED→PAID",
                       code == 200 and res.get("outcome") == "paid"
                       and row2["ステータス"] == "PAID"))
        # 4) 対応行なしは ignored (台帳不変)
        code, res = post({"id": "WH-NOMATCH", "event_type": "INVOICE.PAID",
                          "resource": {"id": "INV2-NOPE-0000", "status": "PAID"}})
        checks.append(("対応行なしは ignored、他行不変",
                       code == 200 and res.get("outcome") == "ignored_no_match"
                       and len(Ledger(ledger_path).rows()) == 2))
        # 5) 不正 JSON は 400
        code, _ = post(None, raw=b"{not json")
        checks.append(("不正 JSON は 400", code == 400))
        # 6) 未対応イベントは ignored
        code, res = post({"id": "WH-OTHER", "event_type": "CHECKOUT.ORDER.APPROVED",
                          "resource": {}})
        checks.append(("未対応イベントは ignored",
                       code == 200 and res.get("outcome") == "ignored_unknown_event"))
        server.shutdown()
        server.server_close()
        all_ok = True
        for label, ok in checks:
            print(f"  [{'OK' if ok else 'NG'}] {label}")
            all_ok = all_ok and ok
        print(f"\n総合: {'ALL OK' if all_ok else '一部 NG'}")
        return 0 if all_ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask2Bill Webhook 受信")
    parser.add_argument("--data-dir", default="demo_data", help="台帳のあるディレクトリ")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--selftest", action="store_true", help="ローカル検証を実行")
    args = parser.parse_args()
    if args.selftest:
        return run_selftest()
    run_server(BASE_DIR / args.data_dir, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
