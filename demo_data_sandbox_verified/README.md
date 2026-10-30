# Sandbox 検証済み実績のみの台帳 (Ask2Bill)

作成: 2026-10-30 17:40 JST ラン (スーパーバイザー)。
目的: 在席セッション・ランブック手順 5 の「検証済みの Sandbox 実績行だけから
再生成する」場合の素材。現行の dashboard.html (モック台帳 3 件) とは別物。

## 出典 (すべて deliverables/rank7-paypal-submission.md §9 の記録による)

| 行 | PayPal 側の事実 | 検証 |
|---|---|---|
| 請求書 INV2-LN49-YAKR-77PE-ZMJM、¥1,000、PAID | 買い手が支払い、API の get で PAID を確認 | Actions ラン #16 (2026-10-30 03:33 JST) |
| 請求書 INV2-EATX-MR4C-TDF9-7CUD、¥150,000、SENT | 作成・送信・取得まで実疎通、支払いリンクを確認 | Actions ラン #13 (2026-10-30 02:36 JST) |
| 注文 20V19863D4727870H、COMPLETED | 買い手の承認後、capture で COMPLETED を確認 | Actions ラン #15 (2026-10-30 03:33 JST) |

## 正直さの注記 (提出時に守ること)

- この CSV は **ライブの API エクスポートではない**。§9 の検証記録から
  編纂したもので、受付番号・受付日時・顧客名・サービスなどの台帳項目は
  空欄のままにしてある (推測で埋めていない)。
- INV2-LN49-YAKR-77PE-ZMJM の支払いリンクは、ラン #13 で別請求書について
  実測確認した payer page パターン (www.sandbox.paypal.com/invoice/p/#<ID>)
  の当てはめであり、LN49 自体のリンクとして個別に記録されたものではない。
- したがってこの dashboard-sandbox-verified.html を提出用に使うか、
  モック台帳である旨を明示したデモ資料として扱うかは、在席セッションで
  決めること。モック行を Sandbox 実績として提示してはいけない。
- 生成: `python3 build_dashboard.py --data-dir demo_data_sandbox_verified --out dashboard-sandbox-verified.html` (3 件)。

## 含めていないもの

- ラン #8 / #9 / #11 の請求書 (INV2-6BUS-2YKG-EK2H-P85S、
  INV2-MMJ8-RMC4-D7B4-GX9F、INV2-44LW-FWER-WTW2-DFTP) は、金額・最終状態が
  §9 に完全には記録されていないため、本台帳には入れていない。
  必要なら §9 の記録を確認してから追加すること (推測で追加しない)。
