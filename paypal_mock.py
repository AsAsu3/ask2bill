#!/usr/bin/env python3
"""
PayPal モックアダプタ (デモ7 Ask2Bill 用)

★これはモックです。実 PayPal への接続・実 API コール・認証情報は一切ありません。★

実 PayPal 開発者プラットフォーム (Sandbox) の以下の API の「形」だけを模しています。
ハッカソン提出前の差し替え作業 (rank7-paypal-submission.md のチェックリスト) で、
このクラスのメソッドを実 Sandbox クライアントに置換すれば本体は無変更で動く設計です。

模している API:
  - Orders API v2:        POST /v2/checkout/orders        -> create_order()
                          POST /v2/checkout/orders/{id}/capture -> capture_order()
  - Invoicing API v2:     POST /v2/invoicing/invoices     -> create_invoice()
                          POST /v2/invoicing/invoices/{id}/send -> send_invoice()
                          GET  /v2/invoicing/invoices/{id}  -> get_invoice()
  - Webhooks (模擬):      PAYMENT.CAPTURE.COMPLETED / INVOICE.PAID 相当の
                          イベントを simulate_payment() が生成する

ID 体系: 実 ID と混同しないよう、すべて "MOCK-" 接頭辞付き。
支払いリンク: https://www.sandbox.paypal.com/invoice/p/#MOCK-... の形を模すが、
              実際に開いても何も起きないダミー文字列です。
"""

from __future__ import annotations

import itertools


class PayPalMockClient:
    def __init__(self) -> None:
        self._order_seq = itertools.count(1)
        self._invoice_seq = itertools.count(1)
        self._capture_seq = itertools.count(1)
        self.orders: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.events: list[dict] = []  # 模擬 Webhook イベントの記録

    # -- Orders API 相当 ------------------------------------------------------
    def create_order(self, amount_jpy: int, currency: str = "JPY",
                     reference_id: str = "") -> dict:
        oid = f"MOCK-ORDER-{next(self._order_seq):04d}"
        order = {
            "id": oid,
            "intent": "CAPTURE",
            "status": "CREATED",
            "purchase_units": [{
                "reference_id": reference_id,
                "amount": {"currency_code": currency, "value": str(amount_jpy)},
            }],
            "links": [{
                "rel": "payer-action",
                "href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid} (MOCK)",
            }],
        }
        self.orders[oid] = order
        return order

    def capture_order(self, order_id: str) -> dict:
        order = self.orders[order_id]
        cid = f"MOCK-CAPTURE-{next(self._capture_seq):04d}"
        order["status"] = "COMPLETED"
        capture = {
            "id": cid,
            "status": "COMPLETED",
            "amount": order["purchase_units"][0]["amount"],
        }
        order.setdefault("captures", []).append(capture)
        self.events.append({
            "event_type": "PAYMENT.CAPTURE.COMPLETED (MOCK)",
            "resource_id": cid,
            "order_id": order_id,
        })
        return capture

    # -- Invoicing API 相当 ---------------------------------------------------
    def create_invoice(self, *, invoicer_name: str, customer_name: str,
                       customer_contact: str, items: list[dict],
                       note: str = "") -> dict:
        """items: [{"name": str, "quantity": "1", "unit_amount_jpy": int}]"""
        iid = f"MOCK-INV2-{next(self._invoice_seq):04d}"
        total = sum(int(i["unit_amount_jpy"]) * int(i.get("quantity", "1"))
                    for i in items)
        invoice = {
            "id": iid,
            "status": "DRAFT",
            "detail": {
                "currency_code": "JPY",
                "note": note,
                "terms_and_conditions": "モック請求書です。実支払いは発生しません。",
            },
            "invoicer": {"name": {"given_name": invoicer_name}},
            "primary_recipients": [{
                "billing_info": {
                    "name": {"given_name": customer_name},
                    "email_address": customer_contact,
                }
            }],
            "items": [{
                "name": i["name"],
                "quantity": str(i.get("quantity", "1")),
                "unit_amount": {"currency_code": "JPY",
                                "value": str(i["unit_amount_jpy"])},
            } for i in items],
            "amount": {"currency_code": "JPY", "value": str(total)},
        }
        self.invoices[iid] = invoice
        return invoice

    def send_invoice(self, invoice_id: str) -> dict:
        invoice = self.invoices[invoice_id]
        invoice["status"] = "SENT"
        invoice["payment_link"] = (
            f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id} (MOCK)")
        return invoice

    def get_invoice(self, invoice_id: str) -> dict:
        return self.invoices[invoice_id]

    # -- 支払いの模擬 (顧客が支払いリンクで支払った想定) -------------------------
    def simulate_payment(self, invoice_id: str) -> dict:
        invoice = self.invoices[invoice_id]
        assert invoice["status"] == "SENT", "未送信の請求書には支払えません"
        order = self.create_order(
            int(invoice["amount"]["value"]),
            reference_id=invoice_id,
        )
        capture = self.capture_order(order["id"])
        invoice["status"] = "PAID"
        invoice["payment_detail"] = {
            "capture_id": capture["id"],
            "order_id": order["id"],
        }
        self.events.append({
            "event_type": "INVOICING.INVOICE.PAID (MOCK)",
            "resource_id": invoice_id,
        })
        return invoice
