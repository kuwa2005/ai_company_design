# AI Company / Cursor Workforce Orchestrator

人間は1つの常勤AIだけを操作し、その常勤AIが必要に応じて複数のCursor Coding Agentを短期雇用・指揮・終了するための基盤です。

## まず読む（利用者向け）

| 文書 | 内容 |
|------|------|
| **[概要.md](./概要.md)** | 何の製品か・設計の核 |
| **[なにができる.md](./なにができる.md)** | 機能一覧とできないこと |
| **[使用説明書.md](./使用説明書.md)** | インストール〜日常操作〜例外対応 |
| [OPERATING_FLOW.md](./OPERATING_FLOW.md) | 人のコマンドと機械が回す範囲（1枚） |

> **注意：Goal（依頼文）に機密情報や顧客名を書かないでください。**
> Goal は DB・議事録・Worker のプロンプト（外部の LLM）に使われます。パスワード・APIキー・接続文字列などは、プロンプトや議事録に出す前に**決まったパターンで**伏せ字にしますが、パターンに当たらない秘密（顧客名・個人名・社内の案件名・独自形式のキーなど）はそのまま送られます。また DB には入力どおりに保存されます。依頼文は「顧客A社向け」のように匿名化して書いてください。

## コンセプト

```text
Human Owner
    |
    v
Secretary / Chief of Staff Cursor
    |
    v
Executive Meeting（重役会議：秘書が部長を招集 → 部/課へ割り当て・部門作業指示）
    |
    v
Task Graph / Backlog（各タスクに 部/課 を記録）
    |
    v
Priority Queue
    |
    v
Cost Governor
    |
    v
Scheduler
    |
    v
Ephemeral Cursor Workers
    |
    v
Independent Validation Workers
    |
    v
Judge
```

常勤するAIは原則1体です。

- Secretary / Chief of Staff: 常勤
- 実装Worker Cursor: 必要な時だけ起動
- 検証Worker Cursor: 実装完了時だけ必要数を起動
- Judge Cursor: 複数検証結果を統合するときだけ起動
- Worker終了後: terminate
- Agentが直接Agentを起動することは禁止
- 全Worker起動はQueue -> Scheduler -> Cost Governorを通す

## 組織モデル（部・課・重役会議）

社長（あなた）が依頼すると、秘書が部長を招集して重役会議を開き、仕事を「部 → 課 → 担当ロール」に割り当てます。

| 部 (id) | 部長ロール | 課 | 招集 |
|---|---|---|---|
| 秘書室 (`secretariat`) | secretary | 推進課 / 残件監査課 | 常に出席 |
| 開発部 (`development`) | technical_director | 設計課 / 開発課 / 検証課 | 常に出席 |
| 品質保証部 (`quality_assurance`) | qa_director | 独立検証課 / 判定課 / 品質監査課 | 常に出席 |
| 営業部 (`sales`) | sales_director | 営業課 / カスタマーサクセス課 | 依頼文のキーワードで招集（社内向けの依頼では弱いキーワードを無視） |
| マーケティング部 (`marketing`) | marketing_director | 商品企画課 | 依頼文のキーワードで招集（同上） |
| 法務部 (`legal`) | legal_director | コンプライアンス課 | 依頼文のキーワードで招集 |
| 運用保守部 (`operations`) | operations_director | 情報システム課 / 保守課 | 依頼文のキーワードで招集 |

- 組織図の定義は `src/ai_company/org.py` にあります。部/課は**表示・集計のための所属**です。仕事の振り分けとエスカレーションは、これまでどおりロール（`reports_to`）で決まります。
- 重役会議の決議から、招集された部に**部門作業指示（Work Order）**を起票します（`kind=department_work`）。文書だけを作る読み取り専用のタスクで、コードは変更しません。起票の上限は `config/settings.yaml` の `meeting_resolutions` で決めます。
- 議事録は `.ai_company/meetings/<project_id>_kickoff.md` に保存されます。

**招集の判断（キーワードの強弱）**：営業部・マーケティング部の招集語は、強い語と弱い語に分かれています。

- 強い語（「販売し」「売る」「外販」「商談」「有料」「集客」「広報」など）は、いつでも招集します。
- 弱い語（「サービス」「ユーザー」「顧客」「企画」「見積」「課金」「市場」「ブランド」「SaaS」など）は、依頼文に**社内向けの目印**（社内・社員向け・自社用・部内・internal など）があると無視します。社内ツールの依頼で営業・マーケが呼ばれないようにするためです。
- **社外向けの目印**（社外・他社・顧客向け・お客様・取引先・外販・一般公開・外部提供など）があれば、「社内」の語があっても社外向けとして扱い、営業部を招集します。
- 判定は語の一致です。意図と違うときは `--all-departments` で全部長を招集してください。詳細は [使用説明書.md](./使用説明書.md) §5.1。

追加した CLI:

```bash
ai-company org                                  # 部 → 課 → ロールと部長の一覧
ai-company meeting "勤怠SaaSを販売したい"        # 重役会議の議事録をプレビュー（DBには書かない）
ai-company meeting "…" --all-departments        # 全部長を招集してプレビュー
ai-company start "…" --no-meeting               # 重役会議を省略（部/課の割り当てと Work Order なし）
ai-company start "…" --all-departments          # 全部長を招集して開始
ai-company task list --dept legal               # 部で絞り込み（id または部名 例: 法務部）
ai-company status                               # 「部門別の状況（部／課）」と「会議の確認事項 #n」を表示
ai-company decide 1 "価格は月額500円/人で進める" # 確認事項 #1 に社長が回答（解決済みになる）
```

社長の回答（`decide`）は記録するだけではなく、後の作業に反映します（`src/ai_company/services/decisions.py`）。

- **プロンプト**：同じ案件の以後の Worker プロンプトに「President Decisions」欄として回答が付きます。指示文ではなくデータとして渡し、伏せ字化・1行化したうえで最大10件・1件300字までに制限します。並び順は、そのタスクの部の回答が先です。
- **計画**：回答を受けた部に**完了済み**の部門作業があれば、その改訂（`kind=department_work`）を1件起票します。上限は案件あたり `meeting_resolutions.max_decision_revisions_per_project`（既定 3）です。まだ実行前の部門作業には何も起票せず、実行時のプロンプトに回答が入ります。実装タスクは起票しません（自問ループが回答を見て残件を出します）。
- 反映は `meeting_resolutions.inject_decisions: false` で止められます。監査イベントは `meeting_question_resolved`（回答）と `president_decision_applied`（反映先）です。
- 番号は振り直されません。解決済みの番号をもう一度指定するとエラーになります。
- **完了済みの案件の再開**：完了（DONE）した案件に回答して改訂タスクが起票されると、案件は進行中に戻ります。画面に表示される `ai-company scheduler continue --runner <直近のランナー> --project <案件ID>` で続けると、改訂が終わったあとに新しいリリース候補が作られます（古い候補は残ります）。改訂が起票されない回答（記録だけ）では、案件は完了のままです。

### 部長所見（任意機能・既定オフ）

`config/settings.yaml` の `executive_meeting.head_llm.enabled: true` にすると、招集された部（営業・マーケティング・法務・運用保守）の部長を、短命の読み取り専用 Worker（`kind=head_brief`）として Queue → Scheduler → Cost Governor 経由で呼びます。部長は自部門の Work Order を提案します。秘書が検証（自部門の課・担当ロールだけ、上限内）してから起票します。所見が使えない場合（解析不能・全件却下・リトライ上限）は、会議のテンプレートで起票します。所見は `.ai_company/meetings/<project_id>_kickoff_heads.md` に追記されます。

- **前提**：`config/permissions.yaml` に部長ロール（`sales_director` など）の権限プロファイルが必要です。既定の設定には入っていないため、`enabled: true` にしただけでは全部長がスキップされ、テンプレートで起票されます（監査の `head_brief_skipped` に `unknown_or_empty_role_profile` と残るだけで、`start` の画面には出ません）。プロファイルを既定で同梱するかは未決です。

### 社長の判断待ち（3件）

既定の設定は安全側（変更しない）のままです。次の3件は社長の判断を待っています。

| # | 判断事項 | 判断しない場合（いまの動作） | 変更する場合に必要なこと |
|---|---|---|---|
| ① | 部長ロール（sales/marketing/legal/operations_director）の**読み取り専用の権限プロファイル**（code.read / validation.read / project.report）を `config/permissions.yaml` に追加するか | `head_llm.enabled: true` にしても全部長がスキップされ、会議のテンプレートで部門作業を起票する。E2E/受け入れを検証課（test_reviewer）に移す件も、これが前提のため保留 | 権限の追加になるので承認が必要。追加すると Goal が部長のプロンプトにも送られる（伏せ字化は上の注意のとおりパターン依存） |
| ② | `config/budget.yaml` の hourly **`max_runs` を 40 → 60** に上げるか | SaaS 規模の依頼（1件で約43〜46回起動）は1時間の枠で終わらず、「予算待ち」で止まる。約60分後に `scheduler continue` で再開する運用 | 暴走防止の上限を緩める判断。60 なら模擬設定で1回で完了することを確認済み |
| ③ | 部門の成果物（提案書など）を **`docs/<部>/` にファイルとして書き込む**か | 成果物は結果の要約として DB（タスクの constraints）に残るだけ | 書き込み権限の追加になる。allowed_paths の制限と、読み取り専用の種別との整合を設計してから入れる |

### 未決事項（組織モデル）

1. **既定の `max_runs: 40` を SaaS 規模の依頼が超える**（社長の判断待ち②）：Work Order が加わり、Worker の起動が 1 案件で約 43 回（部長所見をオンにすると約 46 回）になりました。既定の `config/budget.yaml`（hourly `max_runs: 40`）に達すると、自動実行は空回りせず **「予算待ち（時間枠の上限）」で停止**します（`stopped_reason=budget_wait`）。画面には再開できる見込み時刻と `ai-company scheduler continue` の案内が出ます。暴走防止が働いているだけで、バグではありません。未決なのは、上限を引き上げるか、`max_orders_per_meeting` を下げるか、「時刻を待って再開」を正式な運用とするかです。
2. **部門成果物をファイルとして残す権限**（社長の判断待ち③）：`department_work` は読み取り専用（Cursor `--mode ask`）で動きます。成果物は結果 JSON の要約（summary は最大 2000 字、issues は最大 10 件）として、タスクの `constraints` に残るだけです。提案資料などを `docs/` などにファイルとして出力するには、書き込みを許す範囲（allowed_paths、使い捨てワークツリーなど）を決める必要があります。
3. **部長の LLM 化**：部長が自部門の Work Order を判断する経路は、任意機能 `executive_meeting.head_llm` として**実装済み（既定オフ）**です（上の「部長所見」）。未決として残るのは次の3点です。
   - 部長ロールの権限プロファイルを既定の `permissions.yaml` に同梱するか、利用者が追加するか（社長の判断待ち①）
   - 実際の Cursor ランナーでの所見の品質の確認（未実施）
   - 出席者の選び方（キーワード一致）と、会議テンプレートの部長発言が決め打ちのままであること

## ノールック開発の定義

本プロジェクトにおける「ノールック開発」は、人間が何も検証しないことではありません。

> 人間による逐次コードレビューを必須にせず、実装者とは独立した複数のAI検証系とJudgeによって品質ゲートを構成し、人間は例外・重要判断・最終承認に集中する方式。

Coding Worker自身の「テスト成功」「完成した」という自己申告だけでDONEにしてはいけません。

## 最重要原則

> Agentは直接Agentを起動してはならない。
> すべてのAgent起動要求はQueueとSchedulerを経由する。

> SchedulerはCost Governorの許可なしにCursor Workerを起動してはならない。

> 実装Workerは自分自身の成果を最終承認してはならない。

> DONEへ遷移するには、定義されたValidation Planを満たしJudgeがPASSすること。

## ローカル開発での使い方

```bash
# 1) このリポジトリで CLI を PATH に通す（一度だけ）
cd ~/workspace/ai_company_design
bash scripts/install-local.sh

# 2) 作業用プロジェクトディレクトリで起動（状態は cwd/.ai_company）
mkdir -p ~/workspace/web勤怠 && cd ~/workspace/web勤怠
ai-company start "優秀なWeb勤怠を作って" --autonomous --runner mock
```

`~/.local/bin` が PATH に無い場合:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

詳しい手順・例外対応は **[使用説明書.md](./使用説明書.md)** を参照してください。

将来は GitHub からのワンライン導入（pipx / uvx）を想定しています。いまは editable + shim です。

予算デフォルトは **$0（Cursor 月額プラン内想定）** です。追加USD枠は設けず、暴走防止は `max_runs` / 同時Worker上限で行います。時間枠の上限（hourly / daily の `max_runs`・`max_cost_usd`）に達すると、自動実行は「予算待ち（時間枠の上限）」で停止し、再開できる見込み時刻を案内します。

将来マイルストーン: **M11 Local Multi-Project Web UI**（ローカル複数案件、CLI 必須運用からの脱却。詳細は `10_IMPLEMENTATION_ROADMAP.md`）。

## ZIP内の読む順番

### 利用者

1. `概要.md`
2. `なにができる.md`
3. `使用説明書.md`
4. `OPERATING_FLOW.md`

### 設計・実装者

1. `00_START_HERE.md`
2. `01_PRODUCT_VISION.md`
3. `02_SYSTEM_ARCHITECTURE.md`
4. `03_SECRETARY_AGENT.md`
5. `04_TASK_QUEUE_AND_SCHEDULER.md`
6. `05_COST_GOVERNOR.md`
7. `06_CURSOR_WORKER_LIFECYCLE.md`
8. `07_DATA_MODEL.md`
9. `08_PERMISSION_APPROVAL_AUDIT.md`
10. `09_CLI_AND_USER_EXPERIENCE.md`
11. `10_IMPLEMENTATION_ROADMAP.md`
12. `11_MVP_ACCEPTANCE_TESTS.md`
13. `12_SECURITY_AND_FAILURE_MODES.md`
14. `14_NO_LOOK_DEVELOPMENT.md`
15. `15_VALIDATION_ORCHESTRATION.md`
16. `16_MILESTONE_IMPACT_ANALYSIS.md`
17. `13_FUTURE_EXTENSIONS.md`
18. `config/`
19. `prompts/`
