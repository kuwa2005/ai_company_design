# Secretary / Chief of Staff Agent

## 位置づけ

常勤する唯一の主要AI。
人間はSecretaryだけを操作する。

## 責務

- Goal理解
- Project整理
- 重役会議の招集（部長の選定、部/課への割り当て、部門作業指示の起票）
- Task分解要求
- 必要職種決定
- Staffing Request生成
- Priority調整
- 進捗監視
- 再計画
- Human Escalation
- 日次要約

## できないこと

- Cursor process直接spawn
- hard budget override
- 自己承認
- production直接変更
- Policy変更
- 重役会議を理由にした Worker の直接起動（会議の決議もタスクとして Queue に積むだけ）

## Staffing Request例

```yaml
id: STAFF-000001
role: backend_engineer
requested_count: 3
reason: attendance API implementation
skills: [python, fastapi, postgresql]
lifetime: until_tasks_complete
permissions: [code.edit, test.run]
forbidden: [production.deploy, secrets.read]
```

requested_countは希望値。
実際の起動数はScheduler/Cost Governorが決める。

## Human Report例

```text
09:00 REPORT
Project: Web Attendance
Completed: 84
Running: 4
Queued: 112
Blocked: 2
Cost today: $7.40
Monthly usage: 61%
Human decisions required: 1
```

## Escalation条件

- hard budget超過
- production変更
- customer data
- destructive operation
- 仕様矛盾
- retry超過
- 重大Security問題
- AIのみでは決められない事業判断


## 7. Validationに関する責務

SecretaryはCoding Workerの完了報告だけでTaskをDONEにしてはならない。

実装完了後はTaskのrisk profileに応じたValidation Requestを発行する。
ただしSecretary自身がReviewerを直接spawnしてはならない。

例:

```yaml
validation_request:
  task_id: TASK-00127
  risk: high
  required_reviewers:
    - test
    - spec
    - security
    - edge_case
  judge_required: true
```

人間への報告では、単なる「tests passed」ではなく、Judge判定と未解決リスクを要約する。


## 8. 重役会議（Executive Meeting）

社長の依頼を受けた秘書は、計画（GoalPlan）を作ったあと、部長を招集して重役会議を開きます。会議で各タスクの部/課を決め、その決議どおりにタスクを起票します。

```text
社長の依頼
  → 秘書: understand_goal（計画）
  → 秘書: convene_executive_meeting（招集・割り当て・部門作業指示の決定）※純粋関数
  → 秘書: apply_plan（議事録の部/課をそのままタスクに書き込む）
  → 秘書: record_executive_meeting（議事録ファイル + 監査イベント executive_meeting_held）
  → （head_llm がオンのとき）秘書: issue_head_briefs（対象部の部長所見タスクを起票し、その部のテンプレート指示は保留）
  → 秘書: issue_work_orders（保留していない部門作業指示を起票。冪等）
  → Queue → Scheduler → Cost Governor → Worker
       └ 部長所見の完了時: 検証 → 起票（失敗時は保留していたテンプレートを起票）
  → 社長: decide（確認事項に回答）→ 以後のプロンプトに反映／完了済み部門作業の改訂を起票
       └ 案件が完了済みなら ACTIVE に戻す（project_reopened）
  → 社長: scheduler continue（予算待ち・再オープン後の再開）→ 完了条件を満たせばリリース候補を作り直す
```

全体の流れ（人が打つコマンドと機械の範囲）は [OPERATING_FLOW.md](./OPERATING_FLOW.md) にまとめています。

実装: `src/ai_company/services/executive_meeting.py`、`src/ai_company/services/department_orders.py`、組織図は `src/ai_company/org.py`。

### 8.1 招集の規則

- 毎回出席：秘書室（議長）、開発部、品質保証部。
- 依頼文にキーワードがあるときだけ招集：営業部、マーケティング部、法務部、運用保守部（定義は `org.py` の `keywords` / `weak_keywords`）。
  - **強いキーワード**は常に招集します。例：営業部は「販売し」「外販」「売る」「商談」「有料」、マーケティング部は「マーケ」「集客」「広報」「ランディング」、法務部は「個人情報」「勤怠」「給与」「契約」、運用保守部は「運用」「サーバ」「権限」「インフラ」。
  - **弱いキーワード**（営業部の「販売」「顧客」「見積」「課金」「サービス」、マーケティング部の「ユーザー」「市場」「ブランド」など）は、**社内向けの依頼**では使いません。社内向けの目印は「社内」「社員向け」「自社用」「部内」「internal」などです。ただし、社外向けの目印（「社外」「他社」「顧客向け」「お客様」「取引先」「外部に」「一般向け」など）が1つでもあれば、社外向けとして扱います。
  - 英字のキーワードは単語単位で照合します（`sales` は Salesforce に一致しません）。
- 招集しなかった部は議事録に「欠席（理由）」として記録します（「社内向けの依頼のため」／「該当論点なし」）。欠席した部の観点も、完了前の自問ループでは点検します。
- `--all-departments` を付けると全部長を招集します。`--no-meeting` を付けると会議を省略し、部/課は推定で書き込みます。部門作業指示は起票しません。

### 8.2 議事録に残すもの

- 議題（Goal。見出しの偽装を防ぐため、フェンス付きのコードブロックで囲みます。フェンスは中身にある最長のバッククォート列より長くし、`redact_text` で伏せ字化してから書き出します）
- 出席者と欠席者（理由つき）
- 各部長の発言（現在は定型文）
- タスクの割り当て（タスク → 部/課 → 担当ロール、依存関係）
- 部をまたぐ受け渡し
- 部門作業指示（起票したもの／見送ったものと理由）
- 社長に判断してほしいこと（open questions）

保存先は `.ai_company/meetings/<project_id>_kickoff.md` です。部長所見（head_llm）を有効にした場合は、`<project_id>_kickoff_heads.md` に部長ごとの所見が追記されます。

「社長に判断してほしいこと」は、`ai-company status` に `会議の確認事項 #n` として表示されます（部長所見の確認事項も末尾に加わります）。社長が `ai-company decide <番号> "<回答>"` で回答すると、監査イベント `meeting_question_resolved` に記録され、status から消えます。番号は振り直されません。回答の反映先は §8.5 を参照してください。`ai-company meeting "<依頼>"` を使うと、DB とファイルに書かずにプレビューできます。

### 8.3 部門作業指示（Work Order）

招集された部（`meeting_resolutions.departments` の範囲内）に、決め打ちのテンプレートから文書だけの作業を起票します。

| key | 部 / 課 | 内容 |
|---|---|---|
| `sales.proposal` | 営業部 / 営業課 | 提案資料・デモ手順・価格メモ（案）。中核実装のあと |
| `sales.onboarding` | 営業部 / カスタマーサクセス課 | 導入・問い合わせ動線メモ（`sales.proposal` のあと） |
| `marketing.value` | マーケティング部 / 商品企画課 | 価値仮説・優先機能・非目標 |
| `legal.precheck` | 法務部 / コンプライアンス課 | 法務事前点検（個人情報・労働法制・監査ログ）。最終的な法的判断は専門家・担当部署の確認事項として残す |
| `operations.premise` | 運用保守部 / 情報システム課 | 運用前提・権限・初期データ方針。設計のあと |

- 1会議あたり 4 件、1部あたり 2 件が上限です（既定値）。計画に同じ部/課のタスクがある場合は起票しません。
- `kind=department_work` は読み取り専用です。コードは変更せず、結果 JSON の要約を成果物として残します。検証ゲートには入らず、自問ループで点検します。
- 秘書がこの作業を自分で行うことはなく、Worker を直接起動することもありません。

### 8.3.1 部長所見（head_brief。任意機能・既定オフ）

実装: `src/ai_company/services/head_briefs.py`、設定 `executive_meeting.head_llm`

- オンにすると、対象の部（既定：営業・マーケティング・法務・運用保守）ごとに、部長を読み取り専用の短命 Worker（`kind=head_brief`）として起票します。その部のテンプレート指示は保留します。
- 部長の出力（発言・担当・懸念・確認事項・他部への依頼・Work Order）は、秘書が検証してから起票します。上限は `meeting_resolutions` と共通です。不正な出力、上限超過、解析不能、リトライ上限のときは、保留していたテンプレートで起票します（`fallback: template`）。
- 所見は `<project_id>_kickoff_heads.md` に追記します（伏せ字化とエスケープをしたうえで、行頭に接頭辞を付けます）。確認事項は status の「会議の確認事項」に加わります。
- **待機フォールバック**：実装が完了したあとも所見が実行されない（READY / WAITING / BACKLOG のまま）場合は、Scheduler のサイクルごとに数えます。`max_wait_cycles`（既定 3 回の配車の機会）を超えたら、テンプレートに戻して所見を CANCELLED にします。BLOCKED / FAILED / 予算待ちの所見は、すぐにテンプレートに戻します。これで、部長が動けなくても自問ループとリリースは止まりません。
- 部長ロールの権限プロファイルは、既定の `config/permissions.yaml` に**含まれていません**。プロファイルが無い部長は Policy によってスキップされ、テンプレートで起票されます（§8.6 の未決事項）。

### 8.4 進捗報告での部/課

`ai-company status` の報告に「部門別の状況（部／課）」を追加しました。部/課ごとに、完了・進行中/待ち・停止の件数を表示します。`ai-company task list --dept <部 id または部名>` で絞り込めます。

### 8.5 社長の回答（decide）の反映

実装: `src/ai_company/services/decisions.py`

- **プロンプトへの反映**：同じ案件の以後のすべての Worker プロンプト（検証・判定・自問監査も含む）に「President Decisions」欄を付けます。
  - 回答は**データ**として渡します（指示文として埋め込まない）。伏せ字化と1行化をしたうえで、最大10件・1件300字までです。
  - そのタスクの部の回答を先に並べます。
  - 設定 `meeting_resolutions.inject_decisions: false` で止められます。
- **計画への反映**：回答を受けた部（質問文の先頭の部名から判定）に、**完了済み**の部門作業があれば、その改訂を `kind=department_work` で起票します（**完了済みの部門作業1件につき1件**。営業部のように部門作業が2件 DONE なら改訂も2件）。
  - 改訂タスクの `parent_task_id` は元の部門作業で、部/課・ロールは元と同じです。constraints は `{"work_order_key": "<元のkey>.rev:<回答key>", "decision_key": …, "revises": <元のid>}` です。
  - 上限は案件あたり `meeting_resolutions.max_decision_revisions_per_project`（既定 3、0 で起票しない）です。同じ回答で同じ部門作業を二度改訂することはありません。
  - 実行前・実行中の部門作業には起票しません（実行時のプロンプトに回答が入ります）。部が特定できない確認事項は、プロンプトへの反映だけです。
  - 実装タスクは起票しません。回答と実装の食い違いは、自問ループが回答を見て残件（GAPS）として出します。
- 反映の結果は監査イベント `president_decision_applied`（key、起票した改訂タスク、見送った理由）に残り、`decide` の画面にも「反映先」として表示されます。
- **完了済みの案件への回答**：改訂タスクを起票した結果、案件が DONE だった場合は ACTIVE に戻します（監査イベント `project_reopened`）。`decide` の画面に「案件を再開しました」と、再開のコマンド（`ai-company scheduler continue --runner <直近の runner> --project <id>`）を表示します。改訂が終わると、通常の完了処理でリリース候補を作り直します。
- 制約：回答の key は**質問文のハッシュ**（`q:<12桁>`）です。質問の文言が変わると別の確認事項として扱われます。回答の取り消し（un-decide）はできません。

### 8.5.1 再開（scheduler continue）

秘書が案内する再開は、どの場合も `ai-company scheduler continue` を1回実行するだけです。

| 止まった理由 | 秘書の案内 | 再開後 |
|---|---|---|
| 予算待ち（`stopped_reason=budget_wait`。時間枠の `max_runs` などに到達） | 上限の値と使用量、再開できる見込み時刻、再開のコマンドを表示。「暴走防止の仕組みで、故障ではない」と明記 | 続きから実行します |
| `decide` による再オープン | 再開のコマンドを表示 | 改訂タスクを実行し、リリース候補を作り直します |
| 停止タスク（BLOCKED など） | `status --debug` / `debug task <id>` を案内 | 承認や再実行のあとで再開します |

- 案内のコマンドには `--project <id>` と、直近の実行の runner（mock / cursor）が入ります。
- `--project` を付けずに実行した場合でも、完了条件を満たした ACTIVE の案件はすべて完了処理（リリース候補の作成）を行います。

### 8.6 未決事項

1. **`max_runs: 40` を超える**：Work Order のぶん Worker の起動が増え（mock の E2E で約 43 回）、既定の hourly `max_runs: 40` に達します。このとき自動実行は空回りせず、**「予算待ち」（`stopped_reason=budget_wait`）で停止**し、再開できる見込み時刻と `ai-company scheduler continue` を案内します。上限を引き上げるか、Work Order を減らすか、「時刻を待って再開」を運用とするかは社長の判断待ちです。
2. **部門成果物をファイルとして残す権限**：今は読み取り専用で、成果物は `constraints` の要約だけです。提案資料などをファイルとして残すには、書き込みを許すパスや使い捨てワークツリーを設計する必要があります。
3. **部長の LLM 化**：部長が自部門の Work Order を判断する経路は、任意機能 `executive_meeting.head_llm`（既定オフ）として実装済みです。部長は読み取り専用の短命 Worker（`kind=head_brief`）で、出力は秘書が検証してから起票します。上限は `meeting_resolutions` と共通です。未決として残るのは次の3点です。
   - **招集の判断**（キーワード一致）と、会議テンプレートの部長発言が決め打ちのままであること
   - 部長ロールの権限プロファイルを既定の `permissions.yaml` に同梱するか（無いと全部長がスキップされ、テンプレートで起票される）
   - 実際の Cursor での所見の品質確認
