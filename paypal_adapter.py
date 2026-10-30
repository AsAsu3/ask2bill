#!/usr/bin/env python3
"""
PayPal アダプタ interface (デモ7 Ask2Bill)

receiptdesk.py 本体はこの interface だけに依存する。実装は2つ:

  - MockPayPalAdapter    : paypal_mock.PayPalMockClient を包む検証用実装。
                           ネットワークなしで --demo の一気通貫検証ができる。
                           ★モックは検証専用。ハッカソン提出物には使わない
                           (要件: 実 PayPal Sandbox を中核にすること)★
  - SandboxPayPalAdapter : 実 PayPal 開発者プラットフォーム (Sandbox) 用実装。
                           Orders API v2 / Invoicing API v2 の REST を標準ライブラリ
                           (urllib) で叩く。認証情報は環境変数からのみ読む:
                             PAYPAL_SANDBOX_CLIENT_ID / PAYPAL_SANDBOX_CLIENT_SECRET
                           2026-10-30 に実 Sandbox で検証済み (注文 COMPLETED・
                           請求書 PAID まで API 側で確認。詳細は
                           rank7-paypal-submission.md §9)。

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
INVOICING_SCOPE = "https://uri.paypal.com/services/invoicing"


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
    """実 Sandbox 用 (2026-10-30 に実疎通を検証済み)。

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
        self._invoice_token: str | None = None
        self.invoice_token_scope: str = ""
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

    def _request_full(self, method: str, path: str,
                      body: dict | None = None,
                      token: str | None = None) -> tuple[dict, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {token or self._access_token()}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            headers = dict(resp.headers.items())
            return (json.loads(raw) if raw else {}), headers

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        return self._request_full(method, path, body)[0]

    # Invoicing は既定トークンに scope が付与されない (実測 2026-10-30) ため、
    # invoicing scope を明示要求した専用トークンで呼ぶ。
    def _invoice_access_token(self) -> str:
        if not self._invoice_token:
            self._invoice_token, self.invoice_token_scope = \
                self._fetch_token(INVOICING_SCOPE)
        return self._invoice_token

    def _invoice_request(self, method: str, path: str,
                         body: dict | None = None) -> dict:
        return self._request_full(
            method, path, body, token=self._invoice_access_token())[0]

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
        payload, headers = self._request_full(
            "POST", "/v2/invoicing/invoices", {
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
            }, token=self._invoice_access_token())
        # 実応答は id を含まず、href / Location ヘッダで所在を示す (実測
        # 2026-10-30)。ID を取り出して作成直後の実状態を正規化して返す。
        invoice_id = payload.get("id")
        if not invoice_id and payload.get("href"):
            invoice_id = payload["href"].rstrip("/").split("/")[-1]
        if not invoice_id:
            loc = headers.get("Location") or headers.get("location") or ""
            if loc:
                invoice_id = loc.rstrip("/").split("/")[-1]
        if not invoice_id:
            raise RuntimeError(
                "Invoicing create の応答から請求書 ID を特定できませんでした")
        return self.get_invoice(invoice_id)

    def send_invoice(self, invoice_id):
        return self._invoice_request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/send",
            {"send_to_invoicer": True, "send_to_recipient": True})

    def get_invoice(self, invoice_id):
        invoice = self._invoice_request(
            "GET", f"/v2/invoicing/invoices/{invoice_id}")
        # 本体が使う形へ正規化する。実測 (2026-10-30): 応答 links の rel は
        # self / replace / cancel / remind / record-payment / qr-code のみ。
        # qr-code の href は QR 画像を返す API エンドポイントであり、
        # 顧客が開く支払いページではない (ラン #11 で実物を確認)。
        # 支払いページは PayPal の標準 URL 形式 /invoice/p/#<ID> を
        # API ベースから導いたウェブホストで組み立てる。
        invoice["_top_keys"] = sorted(invoice.keys())
        invoice["_link_rels"] = [link.get("rel", "")
                                  for link in invoice.get("links", [])]
        if "payment_link" not in invoice:
            # API ホストは api / api-m の揺れがあるため文字列置換では
            # 導かず、環境で振り分ける (スタブ検証で置換漏れを実測)
            web_base = ("https://www.sandbox.paypal.com"
                        if "sandbox" in self.base else "https://www.paypal.com")
            invoice["payment_link"] = f"{web_base}/invoice/p/#{invoice['id']}"
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
