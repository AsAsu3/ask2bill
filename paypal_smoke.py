#!/usr/bin/env python3
"""PayPal Sandbox 疎通スモーク (demo7-receiptdesk 用)。

使い方 (認証情報は環境変数からのみ読む。ファイル・ログ・画面出力に
秘密を一切書かないこと):

  # 0) ガード確認 (ネットワークに出ない。認証情報が無い状態の挙動確認)
  python3 paypal_smoke.py --guard

  # 1) OAuth + Orders create + Invoicing create/send/get
  #    (買い手の承認・支払いを伴わない段階まで)
  PAYPAL_SANDBOX_CLIENT_ID=... PAYPAL_SANDBOX_CLIENT_SECRET=... \
    python3 paypal_smoke.py --stage1 --amount 1000 \\
      --buyer-email <Sandbox買い手アカウントのメール> --buyer-name テスト

  # 2) 買い手が承認リンクで承認した後、capture を実行
  PAYPAL_SANDBOX_CLIENT_ID=... PAYPAL_SANDBOX_CLIENT_SECRET=... \
    python3 paypal_smoke.py --capture <ORDER_ID>

  # 3) 請求書の現在ステータス確認 (支払い後は PAID になるはず)
  PAYPAL_SANDBOX_CLIENT_ID=... PAYPAL_SANDBOX_CLIENT_SECRET=... \
    python3 paypal_smoke.py --check-invoice <INVOICE_ID>

  # 4) receiptdesk.py を sandbox アダプタで1件流す
  #    (台帳は demo_data_sandbox/ に分離し、モックの demo_data を汚さない。
  #     Sandbox では支払いシミュレーションは行わず、結果は UNPAID→REMINDED
  #     になるのが正しい。PAID 化は買い手の支払い + Webhook 経路のみ)
  PAYPAL_SANDBOX_CLIENT_ID=... PAYPAL_SANDBOX_CLIENT_SECRET=... \
  RECEIPTDESK_PAYPAL_ADAPTER=sandbox \
    python3 paypal_smoke.py --receiptdesk \\
      --buyer-email <Sandbox買い手アカウントのメール>

出力するもの: 各ステップの成否、Sandbox の注文ID・請求書ID・
ステータス、承認リンク/支払いリンクの有無のみ。アクセストークン、
Client ID / Secret、Authorization ヘッダ、承認リンクのクエリ
(token 等) は一切出力しない。エラー時は PayPal のエラー名と
message のみを要約して出す (debug_id 等の付帯情報は出さない)。

前提: rank7-paypal-submission.md §5 のチェックリスト。
このスクリプト自体は認証情報を取得しない。認証情報がシェルに
届く経路 (Secure Vault 等の承認済み経路) が整った後に使うこと。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paypal_adapter import SandboxPayPalAdapter  # noqa: E402


def _err_text(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return f"HTTP {exc.code} (本文は要約不可)"
        name = payload.get("name", "")
        message = payload.get("message", "")
        if not name and not message:
            # OAuth2 トークンエンドポイントは error / error_description 形式
            name = payload.get("error", "")
            message = payload.get("error_description", "")
        details = payload.get("details", [])
        detail = ""
        if details and isinstance(details, list):
            first = details[0] or {}
            detail = f" / {first.get('issue', '')}: {first.get('description', '')}"
        return f"HTTP {exc.code} {name}: {message}{detail}"[:300]
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _link_present(obj: dict, rel: str) -> bool:
    for link in obj.get("links", []) or []:
        if link.get("rel") == rel:
            return True
    return False


def _guard() -> int:
    has_id = bool(os.environ.get("PAYPAL_SANDBOX_CLIENT_ID"))
    has_secret = bool(os.environ.get("PAYPAL_SANDBOX_CLIENT_SECRET"))
    print(f"PAYPAL_SANDBOX_CLIENT_ID 設定: {'yes' if has_id else 'no'}")
    print(f"PAYPAL_SANDBOX_CLIENT_SECRET 設定: {'yes' if has_secret else 'no'}")
    if not has_id or not has_secret:
        print("RESULT: GUARD-OK (認証情報なしのためネットワークに出ずに停止する)")
        print("  ※ この状態で sandbox 呼び出しをすると明示エラーで止まるのが正しい挙動")
        return 0
    print("RESULT: GUARD-OK (認証情報あり。--stage1 以降で実疎通を行うこと)")
    return 0


def _stage1(args) -> int:
    _cid = os.environ.get("PAYPAL_SANDBOX_CLIENT_ID", "")
    _csec = os.environ.get("PAYPAL_SANDBOX_CLIENT_SECRET", "")
    print(f"[diag] client_id: len={len(_cid)} "
          f"前後空白={'yes' if _cid != _cid.strip() else 'no'} / "
          f"client_secret: len={len(_csec)} "
          f"前後空白={'yes' if _csec != _csec.strip() else 'no'}"
          " (長さと空白の有無のみ。値そのものは出力していない)")
    adapter = SandboxPayPalAdapter()
    try:
        order = adapter.create_order(args.amount, reference_id="receiptdesk-smoke")
    except Exception as exc:  # noqa: BLE001 - 要約して出すため
        print(f"[NG] OAuth/Orders create: {_err_text(exc)}")
        print("RESULT: FAIL")
        return 1
    print("[OK] OAuth トークン取得 (create 呼び出しが成功したことで確認。値は非表示)")
    print(f"[OK] Orders create: id={order.get('id')} status={order.get('status')}")
    print(f"     approve リンク有無: {'yes' if _link_present(order, 'approve') else 'no'}")
    print("     次の手順: 買い手が approve リンクで承認したら "
          f"--capture {order.get('id')} を実行すること")

    if not args.buyer_email:
        print("[SKIP] Invoicing: --buyer-email 未指定のため請求書ステップは省略")
        print("RESULT: PARTIAL-OK (Orders まで成功)")
        return 0
    try:
        invoice = adapter.create_invoice(
            invoicer_name="ReceiptDesk",
            customer_name=args.buyer_name,
            customer_contact=args.buyer_email,
            items=[{"name": "スモークテスト", "quantity": "1",
                    "unit_amount_jpy": args.amount}],
            note="ReceiptDesk Sandbox smoke test",
        )
        print(f"[OK] Invoicing create: id={invoice.get('id')} status={invoice.get('status')}")
        adapter.send_invoice(invoice["id"])
        print("[OK] Invoicing send: 送信済み")
        got = adapter.get_invoice(invoice["id"])
        print(f"[OK] Invoicing get: id={got.get('id')} status={got.get('status')} "
              f"payment_link 有無: {'yes' if got.get('payment_link') else 'no'}")
    except Exception as exc:  # noqa: BLE001
        print(f"[NG] Invoicing: {_err_text(exc)}")
        print("RESULT: PARTIAL-OK (Orders 成功 / Invoicing 失敗)")
        return 1
    print("RESULT: PASS (stage1: OAuth + Orders + Invoicing)")
    return 0


def _capture(args) -> int:
    adapter = SandboxPayPalAdapter()
    try:
        result = adapter.capture_order(args.capture)
    except Exception as exc:  # noqa: BLE001
        print(f"[NG] Orders capture: {_err_text(exc)}")
        print("RESULT: FAIL")
        return 1
    print(f"[OK] Orders capture: id={result.get('id')} status={result.get('status')}")
    print("RESULT: PASS" if result.get("status") == "COMPLETED" else "RESULT: CHECK (COMPLETED以外)")
    return 0 if result.get("status") == "COMPLETED" else 1


def _check_invoice(args) -> int:
    adapter = SandboxPayPalAdapter()
    try:
        got = adapter.get_invoice(args.check_invoice)
    except Exception as exc:  # noqa: BLE001
        print(f"[NG] Invoicing get: {_err_text(exc)}")
        print("RESULT: FAIL")
        return 1
    print(f"[OK] Invoicing get: id={got.get('id')} status={got.get('status')}")
    print("RESULT: PASS" if got.get("status") == "PAID" else "RESULT: CHECK (PAID以外 = 未払い)")
    return 0 if got.get("status") == "PAID" else 1


def _receiptdesk(args) -> int:
    if not args.buyer_email:
        print("[NG] --receiptdesk には --buyer-email が必須 (Sandbox 買い手アカウント)")
        return 2
    inquiry = {
        "channel": "webform", "name": args.buyer_name,
        "contact": args.buyer_email,
        "body": "ウェブサイト制作の見積をお願いします。",
        "owner_decision": "approve", "simulate_payment": False,
    }
    env = dict(os.environ)
    env["RECEIPTDESK_PAYPAL_ADAPTER"] = "sandbox"
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "receiptdesk.py"),
         "--data-dir", "demo_data_sandbox"],
        input=json.dumps(inquiry, ensure_ascii=False) + "\n",
        capture_output=True, text=True, env=env, timeout=120)
    print(proc.stdout)
    if proc.stderr:
        print(f"[stderr] {proc.stderr[:500]}")
    print(f"receiptdesk.py 終了コード: {proc.returncode}")
    print("期待結果: 請求書が実 Sandbox ID で発行され、台帳は UNPAID→REMINDED。"
          "Sandbox では支払いシミュレーションは行わないため PAID にはならない。")
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="PayPal Sandbox 疎通スモーク")
    parser.add_argument("--guard", action="store_true")
    parser.add_argument("--stage1", action="store_true")
    parser.add_argument("--capture", metavar="ORDER_ID")
    parser.add_argument("--check-invoice", metavar="INVOICE_ID")
    parser.add_argument("--receiptdesk", action="store_true")
    parser.add_argument("--amount", type=int, default=1000)
    parser.add_argument("--buyer-email", default="")
    parser.add_argument("--buyer-name", default="テスト")
    args = parser.parse_args()
    if args.guard:
        return _guard()
    if args.stage1:
        return _stage1(args)
    if args.capture:
        return _capture(args)
    if args.check_invoice:
        return _check_invoice(args)
    if args.receiptdesk:
        return _receiptdesk(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
