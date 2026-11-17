# CLI and User Experience

## 最終UX

```bash
ai-company start "優秀なWeb勤怠を作って"
```

## MVP CLI

```bash
ai-company init
ai-company project create "Web attendance"
ai-company task create --project PROJECT-0001 --title "Attendance API" --priority 60
ai-company task list
ai-company scheduler run-once
ai-company worker list
ai-company budget status
ai-company audit list
```

## 現在のCLI（社長の操作）

```bash
ai-company start "Goal" [--autonomous] [--runner mock|cursor] [--no-meeting] [--all-departments]
ai-company status [--project ID]
ai-company decide <番号> "<回答>" [--project ID]
ai-company scheduler continue --runner <mock|cursor> [--project ID]
ai-company ask "今一番問題になっていることは？"
ai-company org
ai-company meeting "Goal" [--all-departments]
```

> **Goal に機密情報・顧客名を書かない。** Goal は DB・議事録・Worker のプロンプト（外部 LLM）に使われる。伏せ字化は決まったパターン（パスワード・APIキー・トークン・接続文字列・暗証番号など）に当たるものだけで、顧客名・個人名・社内の案件名などは伏せ字にならない。DB には入力どおりに保存される。

### 重役会議の招集（キーワードの強弱）

- 秘書室・開発部・品質保証部は常任。営業部・マーケティング部・法務部・運用保守部は依頼文の語で招集する。
- 営業部・マーケティング部の語は「強い語」（販売し・売る・外販・商談・有料・集客・広報など。常に招集）と「弱い語」（サービス・ユーザー・顧客・企画・見積・課金・市場・ブランド・SaaS など）に分かれる。
- 弱い語は、依頼文に社内向けの目印（社内・社員向け・自社用・部内・internal など）があると無視する。欠席理由は「社内向けの依頼のため招集せず」と議事録に残る。
- 社外向けの目印（社外・他社・顧客向け・お客様・取引先・外販・一般公開・外部提供など）があれば、社内向けの目印より優先し、営業部を招集する。
- 判定は語の一致。意図と違えば `--all-departments`。

### 社長の確認事項（decide）と再開

`status` の「人間の判断が必要」に、会議の確認事項が番号つきで出る。

```text
人間の判断が必要: 2（停止・予算待ち 0 / 会議の確認事項 2）
  - 会議の確認事項 #1: 営業部: 価格・契約条件の方針（社長判断）
  - 会議の確認事項 #2: 法務部: 個人情報・法令の扱いの最終確認（専門家／担当部署）
    → 回答: ai-company decide <番号> "回答"（解決済みは表示から外れます）
```

- `decide` の回答は伏せ字化して監査に記録し、同じ案件の以後の Worker プロンプトにデータとして渡す。
- 回答を受けた部の部門作業が完了済みなら、改訂タスクを1件起票する。案件が完了（DONE）していた場合は**進行中に戻し**、再開のコマンドを表示する。
- 再開の案内は `ai-company scheduler continue --runner <直近のランナー> --project <案件ID>` の形で表示する（`status` も同じ）。改訂が終わると新しいリリース候補を作る。
- 番号は振り直さない。解決済みの番号をもう一度指定すると終了コード 2。取り消し（un-decide）は無い。

### 社長の判断（設定・3件、2026-11-18 反映済み）

1. 部長5種と `test_reviewer` の読み取り専用プロファイルを `config/permissions.yaml` に追加。部長所見（`head_llm`）は既定オン。
2. `config/budget.yaml` の hourly `max_runs` を 40 → 60。
3. 部門の成果物を `<案件dir>/docs/<部>/<タスクid>.md` に書き出す（秘書が書く。Worker は読み取り専用のまま）。
4. 設計課に `design_engineer`、検証課に `acceptance_engineer`（E2E・受け入れ）を新設。

詳細は [使用説明書.md](./使用説明書.md) §8.1。

## 将来CLI

```bash
ai-company tell "給与計算機能は今回は不要"
ai-company pause
ai-company resume
ai-company stop
```

## status例

```text
Project: Web Attendance
Status: RUNNING

Tasks:
DONE       83
RUNNING     4
READY      31
WAITING    67
BLOCKED     2

Workers: 4 / 4
Budget Today: $7.40 / $20
Budget Month: $124.30 / $250
Human decisions: 1
```

人間へはTask一覧ではなく、Goal/Progress/Cost/Risk/Blocked/Decisionを優先して見せる。


## 6. Validation CLI

```bash
ai-company validation status TASK-000127
ai-company validation results TASK-000127
ai-company judge show TASK-000127
```

statusでは人間にReviewerの長文をそのまま見せず、次を要約する。

```text
Validation: PASS
Test: PASS
Spec: PASS
Security: PASS
Edge cases: PASS with 1 low-risk note
Judge: PASS
Human action: none
```
