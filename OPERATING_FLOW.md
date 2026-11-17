# 運用フロー（1枚）

人が打つコマンドと、機械が回す範囲の境界。

## 全体の流れ（社長 → 秘書 → 重役会議 → 部/課 → 再開）

```text
社長: start "依頼"
  → 秘書: 計画 → 重役会議（部長を招集）→ 議事録 .ai_company/meetings/<id>_kickoff.md
  → 各タスクに 部/課 を記録して起票
  → 招集した部に部門作業指示（Work Order, kind=department_work, 文書のみ）を起票
  → （head_llm オン時）部長所見（head_brief）→ 秘書が検証して Work Order を起票／失敗・待ちすぎはテンプレートへ
  → Queue → Scheduler → Cost Governor → Worker → 独立検証 → Judge
  → 実装キュー空 → 部長所見・部門作業の完了を待つ → 自問ループ（部門別の監査）→ リリース候補
社長: status で「会議の確認事項 #n」を見る → decide n "回答"
  → 以後のプロンプトに反映／完了済みの部門作業の改訂を起票（完了済みの案件なら再オープン）
社長: 止まったら（予算待ち・再オープン）→ 案内どおり scheduler continue で再開
```

## 日常のコマンド

```text
ai-company doctor          # 事前診断（副作用なし）
ai-company meeting "Goal" [--all-departments]   # 重役会議のプレビュー（DB に書かない）
ai-company start "Goal" [--autonomous] [--runner cursor|mock] [--delivery-mode strict|prototype]
                        [--no-meeting] [--all-departments]
ai-company scheduler continue [--runner cursor|mock] [--project ID]
ai-company status [--project ID] [--debug]
ai-company decide <番号> "<回答>" [--project ID]  # 会議の確認事項に社長が回答
ai-company org / task list [--dept 部]           # 組織図・部別のタスク
ai-company approval … / fire-supervisor …        # 例外時のみ
```

| コマンド | 人の役割 | 機械の役割 |
|---|---|---|
| `doctor` | 環境を確認 | PASS/WARN/FAIL と直し方を表示 |
| `meeting` | 依頼の前に会議を確かめる | 招集・割り当て・Work Order・確認事項を表示（副作用なし） |
| `start` | Goal を与える | 重役会議 → 部/課つきの Task Graph・Work Order・人員依頼を作る（ワーカーは起動しない）。`--autonomous` でそのまま自動実行 |
| `scheduler continue` | 進める合図・再開 | Lease → Cost → Worker（mock/cursor）→ Validation → Judge → 自問。完了条件を満たした案件はリリース候補を作る |
| `status` | 読む | 実装消化／自問中／リリース可、部門別の状況、「人間の判断が必要」（停止・予算待ち・会議の確認事項 #n）、再開の案内 |
| `decide` | 確認事項に回答 | 回答を記録し、以後のプロンプトへ反映。完了済みの部門作業の改訂を起票し、必要なら案件を再オープンして再開コマンドを表示 |
| `approval` | 例外を承認/却下 | 停止タスクを解除 |
| `fire-supervisor` | わからず屋を解雇 | 出荷優先で DONE＋残件引き継ぎ |

## 止まったときと再開

| 状況 | 表示 | 人がすること |
|---|---|---|
| 予算待ち（時間枠の `max_runs` などに到達） | 上限と使用量、再開できる見込み時刻、再開コマンド | 時刻を待って `scheduler continue …` を1回実行（または `config/budget.yaml` の上限を見直す） |
| 会議の確認事項が残っている | status の「会議の確認事項 #n」 | `decide n "回答"`（答えなくても実装は進む） |
| 完了後に `decide` で改訂が出た | 「案件を再開しました」と再開コマンド | 表示されたコマンドを実行 → 改訂後にリリース候補を作り直す |
| 停止タスク（BLOCKED） | `status --debug` / `debug task <id>` の案内 | 承認・却下、または再実行のあとで再開 |

再開コマンドには `--project <id>` と直近の runner が入ります。部長所見が動けない場合（権限プロファイルなし・待ちすぎ）は、自動でテンプレートに戻るため、人の操作は要りません。

## 完了の意味

- **実装キュー空** ≠ Goal 完了
- **自問ループ CLEAR まで** = リリース候補（部長所見と部門作業が終わってから自問ループに入る）
- リリース候補のあとに社長の回答で改訂が出たら、案件は進行中に戻り、改訂後に候補を作り直す
- `ship_first` / `prototype` で無視した FAIL は **残件タスク** に必ず残る
- デフォルトは `strict`（安全側）。デモは `--delivery-mode prototype`

## 検証の分担

| 役割 | 意味 |
|---|---|
| `test_reviewer` 等 | 実装ゲート（この差分は出荷してよいか） |
| `test_auditor` | メタ監査（緑でもテストが正しいか） |
| Judge hard rules | コード側の最終出荷判定 |

## デフォルト方針

- `delivery_mode: strict`
- `auto_fire_stubborn_supervisor: false`
- Reviewer / Judge は write 禁止（git diff で強制）
- Worker 自己申告の `changed_files` は Evidence Collector が照合
