# デモ7: ReceiptDesk Agent (PayPal AI Hackathon 提出プロトタイプ)

提出先: PayPal AI Hackathon (Devpost、締切 **2026-11-13 05:00 JST**、目標提出 11/10)。
提出文ドラフト: `../deliverables/rank7-paypal-submission.md`

コンセプト: 小規模事業者向けの「受付エージェント」。問い合わせを受けると
(a) サービスを分類して見積ドラフトを作り、(b) オーナー承認を経て初めて請求書を発行し、
(c) 入金を確認して台帳と領収連絡まで一気通貫で行う。

土台は Rank 2 の資産: demo-bot (FAQ/受付) と demo2-pipeline (受付→分類→台帳→通知)。
本デモの新規部分は「見積 → オーナー承認ゲート → 請求レイヤー」のみ。

## ★重要: 決済レイヤーの構成 (モックは検証専用)★

ハッカソン要件は「PayPal 開発者プラットフォーム (無料 Sandbox) を中核に組み込み、
動作するプロトタイプであること (モックアップ不可)」。したがって構成は:

- 本体 (`receiptdesk.py`) は `paypal_adapter.py` の **interface だけ**に依存する。
- `MockPayPalAdapter` (既定): Orders / Invoicing API の形を模した検証用モック。
  ネットワークなしで `--demo` の一気通貫検証ができる。ID は全て `MOCK-` 接頭辞。
  **提出物には使わない。**
- `SandboxPayPalAdapter`: 実 Sandbox 用実装。Orders API v2 の create/capture、
  Invoicing API v2 の作成/送信/取得を REST (標準ライブラリ urllib) で実装済み。
  認証情報は環境変数 `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_CLIENT_SECRET`
  からのみ読む。**Sandbox アプリと認証情報は発行済み（認証情報は公開しない）。
  実接続スモークテストは実施準備中** — 登録手順と
  疎通スモークのチェックリストは `../deliverables/rank7-paypal-submission.md` §5。
- 切替は環境変数 `RECEIPTDESK_PAYPAL_ADAPTER=mock|sandbox` のみ。本体の変更は不要。
  認証情報が無い状態で sandbox を呼ぶと、ネットワークに出る前に明示エラーで止まる
  (確認済み)。

## 安全設計 (中核アピール)

- AI は見積「ドラフト」まで。`owner_decision = approve` が無い限り請求書は発行されない
  (stdin モードの既定は pending = 承認待ちで停止する)。
- オーナーが差し戻した見積は台帳に `REJECTED_BY_OWNER` と記録され、請求番号は空のまま。
- 台帳 (invoices.csv) が唯一の真実。ステータス遷移:
  DRAFT_ESTIMATE → AWAITING_OWNER / REJECTED_BY_OWNER → UNPAID → PAID / REMINDED。

## セットアップと実行

- Python 3.10+ (標準ライブラリのみ、`pip install` 不要、ネットワーク不要)

```sh
cd ~/workspace/earn-with-muse/demo7-receiptdesk
python3 receiptdesk.py --demo    # 3シナリオ一気通貫 + 自己検証

# 標準入力モード (1行1件 JSON)。owner_decision を省略すると承認待ちで止まる
echo '{"channel":"webform","name":"山田","contact":"a@example.com","body":"ウェブサイト制作の見積がほしい","owner_decision":"approve","simulate_payment":true}' \
  | python3 receiptdesk.py
```

## デモの3シナリオ (`--demo`)

| # | 内容 | 結果 |
|---|---|---|
| 1 | ウェブサイト制作の見積依頼 → オーナー承認 → 請求発行 → 顧客が支払い (模擬 Webhook) | 台帳 PAID、モック注文 ID と支払完了日時を記録、領収連絡を生成 |
| 2 | 保守プランの問い合わせ → オーナーが見積を差し戻し | `REJECTED_BY_OWNER`、請求書は発行されない |
| 3 | データ分析レポート (「至急」→ 優先度『高』) → 承認 → 請求 → 未払い | `REMINDED`、リマインド文を生成、支払いリンクは台帳に保持 |

## ファイル構成

```
demo7-receiptdesk/
├── receiptdesk.py   # 本体: 受付→分類→見積→承認ゲート→請求→入金確認
├── paypal_adapter.py # PayPal interface + Mock / Sandbox の2実装 (切替は環境変数)
├── paypal_mock.py   # モックの実体 (Orders / Invoicing / Webhook の形を模す)
├── catalog.json     # サービスカタログ (基本価格・分類キーワード・優先度キーワード)
├── templates.json   # 見積案内・承認依頼・請求案内・領収・リマインドの文面
├── demo_data/       # --demo の実行データ (invoices.csv / sequence.json)
└── README.md
```

## 検証ログ

2026-10-24 (JST)、`python3 receiptdesk.py --demo` を実行 (終了コード 0)。
3シナリオが想定どおりに遷移し、自己検証 8 項目すべて OK:

```
台帳: INQ-...-0001 ウェブサイト制作 ¥150,000 請求 MOCK-INV2-0001 PAID
      INQ-...-0002 保守プラン       ¥30,000  請求なし           REJECTED_BY_OWNER
      INQ-...-0003 データ分析レポート ¥50,000 請求 MOCK-INV2-0002 REMINDED

  [OK] シナリオ1: 承認→請求→入金で PAID になった
  [OK] シナリオ1: モック注文IDと支払完了日時が台帳に記録された
  [OK] シナリオ1: 請求金額がカタログ価格 (¥150,000) と一致
  [OK] シナリオ2: 差し戻しでは請求書が発行されていない
  [OK] シナリオ3: 未払いは REMINDED で、支払いリンクが台帳に残る
  [OK] シナリオ3: 「至急」で優先度『高』に分類された
  [OK] 台帳は3件のみ (重複・余分な請求なし)
  [OK] モック Webhook イベントが記録された (PAYMENT.CAPTURE / INVOICE.PAID)
総合: ALL OK
```

追加確認: 標準入力モードで `owner_decision` を省略した投入は
`AWAITING_OWNER` で停止し請求書を発行しないこと、`--demo` の再実行も
終了コード 0 で再現することを確認済み。アダプタ interface 分離後
(2026-10-24) にも `--demo` を再実行し ALL OK (終了コード 0) を再確認した。

## 検証済み / 未検証の切り分け

| 部分 | 状態 |
|---|---|
| 受付→分類→見積→承認ゲート→台帳→通知の全フロー | ✅ 検証済み (`--demo` ALL OK) |
| モックアダプタでの請求・入金・Webhook イベント | ✅ 検証済み (同上) |
| アダプタ interface 分離・環境変数での切替 | ✅ 検証済み (sandbox 選択・認証情報なしガードを確認) |
| Sandbox の OAuth / Orders create・capture / Invoicing 送信 | ⏳ コード実装済み・**未検証** (認証情報の発行待ち、§5 の手順で実施) |
| Webhook 受信エンドポイント (台帳への自動反映) | ⏳ 未実装 (公開 URL が必要。提出工程 W2 で実装) |
| AG Grid ダッシュボード | ⏳ 未実装 (現状は CSV 台帳 + コンソール表示。W2 で実装) |

## 制限事項 (プロトタイプの範囲)
- 分類はキーワード方式 (demo2 と同じ)。提出版では LLM 分類への差し替えが可能だが、
  金額の確定を AI に委ねない設計 (承認ゲート) は維持する。
- 個人情報 (連絡先) の保存期間・台帳の共有範囲は導入時に合意すること。
- 収益・受賞を保証するものではない。AI 利用は提出文にも明記する。
