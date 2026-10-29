#!/usr/bin/env python3
"""
PayPal アダプタ interface (デモ7 ReceiptDesk Agent)

receiptdesk.py 本体はこの interface だけに依存する。実装は2つ:

  - MockPayPalAdapter    : paypal_mock.PayPalMockClient を包む検証用実装。
                           ネットワークなしで --demo の一気通貫検証ができる。
                           ★モックは検証専用。ハッカソン提出物には使わない
                           (要件: 実 PayPal Sandbox を中核にすること)★
  - SandboxPayPalAdapter : 実 PayPal 開発者プラットフォーム (Sandbox) 用実装。
                           Orders API v2 / Invoicing API v2 の REST を標準ライブラリ
                           (urllib) で叩く。認証情報は環境変数からのみ読む:
                             PAYPAL_SANDBOX_CLIENT_ID / PAYPAL_SANDBOX_CLIENT_SECRET
                           ★現時点では認証情報が無いため未検証★。認証情報が揃ったら
                           rank7-paypal-submission.md §5 のスモーク手順で検証する。

切替: 環境変数 RECEIPTDESK_PAYPAL_ADAPTER=mock|sandbox (既定 mock)。

interface が模す/使うエンドポイント:
  create_order   POST /v2/checkout/orders
  capture_order  POST /v2/checkout/orders/{id}/capture
  create_invoice POST /v2/invoicing/invoices
  send_invoice   POST /v2/invoicing/invoices/{id}/send
  get_invoice    GET  /v2/invoicing/invoices/{id}
  settle_invoice 支払いの確定。モックでは模擬 Webhook で PAID 化する。
                 Sandbox では支払者本人が承認リンクで支払い、確定は Webhook
                 (PAYMENT.CAPTURE.COMPLETED 等) で台帳へ反映するのが実フロー
                 のため、本メソッドは状態確認のみで送金操作はしない。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request

from paypal_mock import PayPalMockClient

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


class PayPalAdapter:
    """本体が依存する interface。実装はこのメソッド群をすべて備えること。"""

    is_mock: bool = False
    name: str = "base"
    events: list = []  # 支払いイベントの記録 (モックでは模擬 Webhook)

    def create_order(self, amount_jpy: int, currency: str = "JPY",
                     reference_id: str = "") -> dict:
        raise NotImplementedError

    def capture_order(self, order_id: str) -> dict:
        raise NotImplementedError

    def create_invoice(self, *, invoicer_name: str, customer_name: str,
                       customer_contact: str, items: list[dict],
                       note: str = "") -> dict:
        raise NotImplementedError

    def send_invoice(self, invoice_id: str) -> dict:
        raise NotImplementedError

    def get_invoice(self, invoice_id: str) -> dict:
        raise NotImplementedError

    def settle_invoice(self, invoice_id: str) -> dict:
        raise NotImplementedError


class MockPayPalAdapter(PayPalAdapter):
    """検証用モック。実接続なし。提出には使わない。"""

    is_mock = True
    name = "mock"

    def __init__(self) -> None:
        self._client = PayPalMockClient()

    @property
    def events(self) -> list:  # type: ignore[override]
        return self._client.events

    def create_order(self, amount_jpy, currency="JPY", reference_id=""):
        return self._client.create_order(amount_jpy, currency, reference_id)

    def capture_order(self, order_id):
        return self._client.capture_order(order_id)

    def create_invoice(self, **kwargs):
        return self._client.create_invoice(**kwargs)

    def send_invoice(self, invoice_id):
        return self._client.send_invoice(invoice_id)

    def get_invoice(self, invoice_id):
        return self._client.get_invoice(invoice_id)

    def settle_invoice(self, invoice_id):
        return self._client.simulate_payment(invoice_id)


class SandboxPayPalAdapter(PayPalAdapter):
    """実 Sandbox 用 (未検証: 認証情報の発行待ち)。

    認証情報は環境変数からのみ読む。コード・台帳・ログに秘密を書かないこと。
    ネットワーク呼び出しはメソッド実行時にのみ行う (import/初期化では行わない)。
    """

    is_mock = False
    name = "sandbox"

    def __init__(self) -> None:
        self.client_id = os.environ.get("PAYPAL_SANDBOX_CLIENT_ID", "")
        self.client_secret = os.environ.get("PAYPAL_SANDBOX_CLIENT_SECRET", "")
        self.base = os.environ.get("PAYPAL_SANDBOX_BASE", SANDBOX_BASE)
        self._token: str | None = None
        self.token_scope: str = ""
        self.events: list = []

    # -- REST 基盤 -------------------------------------------------------------
    def _require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "PayPal Sandbox 認証情報が未設定です "
                "(PAYPAL_SANDBOX_CLIENT_ID / PAYPAL_SANDBOX_CLIENT_SECRET)。"
                "rank7-paypal-submission.md §5 の登録手順で発行してください。")

    def _fetch_token(self, scope: str | None = None) -> tuple[str, str]:
        """トークンを新規取得し (トークン, 付与された scope 文字列) を返す。

        scope 文字列は能力の識別子であり秘密ではない (診断出力可)。
        トークン値そのものは呼び出し側でも出力しないこと。
        """
        self._require_credentials()
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = "grant_type=client_credentials"
        if scope:
            body += "&scope=" + urllib.parse.quote(scope, safe="")
        req = urllib.request.Request(
            f"{self.base}/v1/oauth2/token",
            data=body.encode(),
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        return payload["access_token"], payload.get("scope", "")

    def _access_token(self) -> str:
        if self._token:
            return self._token
        self._token, self.token_scope = self._fetch_token()
        return self._token

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    # -- Orders API v2 ----------------------------------------------------------
    def create_order(self, amount_jpy, currency="JPY", reference_id=""):
        return self._request("POST", "/v2/checkout/orders", {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id,
                "amount": {"currency_code": currency, "value": str(amount_jpy)},
            }],
        })

    def capture_order(self, order_id):
        return self._request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", {})

    # -- Invoicing API v2 --------------------------------------------------------
    def create_invoice(self, *, invoicer_name, customer_name, customer_contact,
                       items, note=""):
        return self._request("POST", "/v2/invoicing/invoices", {
            "detail": {"currency_code": "JPY", "note": note},
            "invoicer": {"name": {"given_name": invoicer_name}},
            "primary_recipients": [{
                "billing_info": {
                    "name": {"given_name": customer_name},
                    "email_address": customer_contact,
                }}],
            "items": [{
                "name": i["name"],
                "quantity": str(i.get("quantity", "1")),
                "unit_amount": {"currency_code": "JPY",
                                "value": str(i["unit_amount_jpy"])},
            } for i in items],
        })

    def send_invoice(self, invoice_id):
        return self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/send",
            {"send_to_invoicer": True, "send_to_recipient": True})

    def get_invoice(self, invoice_id):
        invoice = self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")
        # 本体が使う形へ正規化する (payment_link を links から拾う。未検証:
        # Sandbox 疎通時に実レスポンスで rel 名を確認して合わせること)。
        if "payment_link" not in invoice:
            for link in invoice.get("links", []):
                if link.get("rel") in ("payer-view", "payer_view"):
                    invoice["payment_link"] = link.get("href", "")
                    break
            invoice.setdefault("payment_link", "")
        return invoice

    def settle_invoice(self, invoice_id):
        """Sandbox では送金操作はしない。支払いは支払者＋Webhook で確定する。
        ここでは現在の状態を返し、台帳反映は Webhook 受信側の責務とする。"""
        invoice = self.get_invoice(invoice_id)
        self.events.append({
            "event_type": "INVOICE.STATUS_CHECK (sandbox)",
            "resource_id": invoice_id,
            "status": invoice.get("status", ""),
        })
        return invoice


def get_adapter(name: str | None = None) -> PayPalAdapter:
    """環境変数 RECEIPTDESK_PAYPAL_ADAPTER (既定 mock) で実装を選ぶ。"""
    selected = (name or os.environ.get(
        "RECEIPTDESK_PAYPAL_ADAPTER", "mock")).strip().lower()
    if selected == "mock":
        return MockPayPalAdapter()
    if selected == "sandbox":
        return SandboxPayPalAdapter()
    raise ValueError(f"未知のアダプタ: {selected} (mock|sandbox)")
