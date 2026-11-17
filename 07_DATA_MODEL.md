# Data Model

## Project

```text
id
name
goal
status
budget_soft
budget_hard
created_at
```

## Task

```text
id
project_id
parent_task_id
title
objective
role_required
department_id   # 部 id（例: development / legal）。表示・集計用。NULL 可
section_id      # 課 id（例: build / compliance）。NULL = 部長直轄
status
priority
constraints
acceptance_criteria
allowed_paths
forbidden_paths
retry_count
max_retries
estimated_cost
created_at
updated_at
```

## TaskDependency

```text
task_id
depends_on_task_id
```

## Worker

```text
id
role
project_id
task_id
status
pid
hired_at
terminated_at
cost_estimated
cost_actual
```

## Execution

```text
id
task_id
worker_id
started_at
finished_at
exit_code
stdout_path
stderr_path
success
estimated_cost
actual_cost
runner_kind     # "cursor" / "mock"。status の再開案内で直近のランナーを推定する（secretary.infer_runner）
```

- `runner_kind` は、以前は mock 実行で NULL のままでした。現在は mock 実行でも `"mock"` を記録します。古い DB の NULL の行しか無い案件は、推定が既定の `cursor` になります。

## StaffingRequest

```text
id
project_id
role
requested_count
approved_count
reason
status
created_at
```

## BudgetEntry

```text
id
scope_type
scope_id
amount
kind  # reserve/spend/release/adjustment
created_at
```

## Approval

```text
id
action
requested_by
approved_by
status
reason
created_at
resolved_at
```

## AuditEvent

```text
id
event_type
actor_type
actor_id
target_type
target_id
metadata
created_at
```

## Artifact

```text
id
project_id
task_id
kind
path
metadata
created_at
```

## DB Tables

```text
projects
tasks
task_dependencies
staffing_requests
workers
executions
budget_entries
approvals
audit_events
artifacts
```


## 8. Validation Plan

```python
class ValidationPlan:
    id: str
    task_id: str
    risk_level: str
    required_review_types: list[str]
    judge_required: bool
    status: str
```

## 9. Validation Result

```python
class ValidationResult:
    id: str
    plan_id: str
    task_id: str
    review_type: str
    worker_id: str
    execution_id: str
    verdict: str  # PASS / FAIL / UNCERTAIN
    findings: list[dict]
    evidence: list[dict]
    confidence: float | None
    created_at: datetime
```

## 10. Judge Decision

```python
class JudgeDecision:
    id: str
    plan_id: str
    task_id: str
    verdict: str  # PASS / REJECT / HUMAN_ESCALATION
    reasons: list[str]
    conflicting_reviews: list[str]
    created_at: datetime
```

## 11. Task追加フィールド

```text
kind
risk_level
validation_plan_id
source_execution_id
```

## 12. DB追加Tables

```text
validation_plans
validation_results
judge_decisions
```

## 13. 組織（部・課）と重役会議

### 13.1 Task の部/課（`tasks.department_id` / `tasks.section_id`）

```text
department_id  String(64)  NULL 可  index ix_tasks_department_id
section_id     String(64)  NULL 可
```

- 値は `src/ai_company/org.py` の `DEPARTMENTS` の id です（DB にマスタ表はありません）。
- 仕事の所属（どの部のどの課の仕事か）を表すだけです。Worker の振り分けとエスカレーションは、これまでどおり `role_required` とロールの `reports_to` で決まります。
- 重役会議を開いた場合、秘書は議事録の割り当て（`Assignment.department_id` / `section_id`）をそのまま書き込みます。そのため議事録と DB は一致します。会議を開かない場合（`--no-meeting`）や自問ループで起票したタスクは、タイトル・ロール・種別から推定して書き込みます。
- 移行：起動時に `migrate_additive_columns` が列と索引を追加し、`backfill_task_org_units` が NULL の行を推定で埋めます。alembic のリビジョン `0002_task_org_units` も同じ処理です。旧タスクの部/課は推定値なので、当時の議事録と一致するとは限りません。

部と課の一覧（id）:

| department_id | 部 | section_id |
|---|---|---|
| `secretariat` | 秘書室 | `pmo` 推進課, `completeness` 残件監査課 |
| `development` | 開発部 | `design` 設計課, `build` 開発課, `verification` 検証課 |
| `quality_assurance` | 品質保証部 | `independent_review` 独立検証課, `judgement` 判定課, `quality_audit` 品質監査課 |
| `sales` | 営業部 | `sales_proposal` 営業課, `customer_success` カスタマーサクセス課 |
| `marketing` | マーケティング部 | `product_planning` 商品企画課 |
| `legal` | 法務部 | `compliance` コンプライアンス課 |
| `operations` | 運用保守部 | `it_systems` 情報システム課, `maintenance` 保守課 |

### 13.2 部門作業指示（Work Order）＝ `kind = department_work`

重役会議の決議から、招集された部に起票するタスクです。専用のテーブルは作らず、`tasks` の1行として表します。

```text
kind                 department_work
risk_level           low
department_id        sales / marketing / legal / operations（meeting_resolutions.departments の範囲内）
section_id           テンプレートの課
priority             min(テンプレート値, meeting_resolutions.priority_ceiling)
constraints (JSON)   {"work_order_key": "legal.precheck", "meeting": "<会議タグ>"}
                     完了後に追記: work_order_summary（伏せ字化して2000字まで）,
                                 work_order_issues（伏せ字化して10件・各300字まで）,
                                 work_order_ignored_proposals（無視した proposed_tasks の件数）
                     社長回答による改訂（§13.4）: {"work_order_key": "<元のkey>.rev:<回答key>",
                                 "decision_key": "q:…", "revises": "<元の task id>"}
```

- `work_order_summary` / `work_order_issues` は、ワーカーの出力を `redact_text` で**伏せ字化してから保存**します（`record_department_work_result`）。読み出し側（自問監査のプロンプト）でも、もう一度伏せ字化します。
- 部長所見（§13.3）から起票した Work Order と、社長回答による改訂の objective の末尾には、文書だけを作るための規則（`DOC_RULE`：「成果物は文書のみ。コードやリポジトリは変更しない。結果 JSON の summary に要点、issues に未確定事項を書く。」）が付きます。

- 冪等性：同じ案件で `work_order_key` が既にあれば、再び起票しません。
- 読み取り専用の種別として、Cursor は `--mode ask` で動きます。検証ゲート（Validation Plan / Judge）には入りません。完了すると DONE になり、自問ループで点検されます。
- 結果の `proposed_tasks` は無視します。部門作業が連鎖してタスクが増えることはありません。
- 未完了の `department_work` があるあいだは、自問ループ（closure）を起票しません。

上限設定（`config/settings.yaml` → `meeting_resolutions`）:

```yaml
meeting_resolutions:
  enabled: true
  max_orders_per_meeting: 4
  max_orders_per_department: 2
  max_estimated_cost_per_meeting_usd: 0.0   # 0 = 件数上限のみ
  priority_ceiling: 80
  departments: [legal, sales, marketing, operations]
```

上限を超えた指示や、計画に同じ部/課のタスクがある指示は起票しません。その理由は議事録と監査ログに残します。

### 13.3 部長所見 ＝ `kind = head_brief`（任意機能・既定オフ）

`config/settings.yaml` の `executive_meeting.head_llm.enabled: true` のときだけ起票します。専用のテーブルは作らず、`tasks` の1行として表します。

```text
kind                 head_brief（TaskKind.HEAD_BRIEF。読み取り専用、検証ゲートなし）
role_required        部長ロール（sales_director / marketing_director / legal_director / operations_director）
department_id        対象の部
section_id           NULL（部長直轄）
priority             98（計画タスクより先に動く。並行で実行される）
constraints (JSON)   起票時: {"meeting": "<会議タグ>", "brief_for": "<部 id>", "goal": "<依頼>",
                             "held_orders": [保留したテンプレート Work Order]}
                     適用後: brief_applied, head_statement（伏せ字化して400字まで）,
                             head_open_questions, head_handoff_requests（伏せ字化）,
                             head_issued（起票した key）, head_rejected（却下した key）
                     フォールバック時: fallback_applied, fallback_reason
                     待機中: wait_cycles（未着手のまま数えた回数。RUNNING になると削除）
```

- 部長の出力は、秘書が検証（自部門の課・担当ロールだけ、`meeting_resolutions` の上限内）してから Work Order として起票します。部長が他部のタスクを起票することはできません。
- 解析不能、全件却下、リトライ上限のいずれかの場合は、保留していたテンプレートの Work Order を起票します（`fallback: template`）。部長は `reports_to` の経路に入らないため、上申はしません。
- 部長ロールの権限プロファイルが `config/permissions.yaml` に無い場合は起票せず、その部はすぐテンプレートで起票します（`head_brief_skipped`）。
- 起動されないまま残った所見の扱い（`head_briefs.settle_stalled_head_briefs`）。実装が完了したあと、自問ループの起票判定のたびに（Scheduler の1サイクルに1回、配車の前に）評価します。
  - BLOCKED / FAILED / DEFERRED_BUDGET → すぐにテンプレートへフォールバックし、CANCELLED にします。
  - READY / WAITING / BACKLOG → `constraints.wait_cycles` を1増やし、**`wait_cycles > head_llm.max_wait_cycles`**（既定 3）になったらフォールバックします。N は「配車の機会の回数」で、0 ならすぐフォールバックします。監査の reason は `stalled_waiting:<status>:<n>cycles` です。
  - RUNNING → 数えず、`wait_cycles` を削除します（リトライで READY に戻ったら数え直し）。

### 13.4 社長の回答（decide）

専用のテーブルは作らず、監査イベントとして記録します。

- `meeting_question_resolved`（target=project）の metadata：`number`、`question`、`answer`（伏せ字化して500字まで）、`question_key`（質問文の SHA-1 の先頭12桁で `q:<hex>`）、`department_id`（質問文の先頭の部名から推定。分からなければ NULL）
- key の無い古い行は、読み出し時に質問文から key と部を補います（`decisions.president_decisions`）。
- 回答の反映（`decisions.apply_president_decision`）は `president_decision_applied` に記録します。改訂タスクの constraints は §13.2 のとおりです。
- 改訂タスクを1件以上起票し、かつ案件が DONE だった場合は、`projects.status` を ACTIVE に戻し、`project_reopened` を記録します。
- 案件の完了判定（`autonomous._project_complete`）は、同じ案件に未完了（DONE / CANCELLED 以外）の `department_work` / `head_brief` が無いことも条件にしています。改訂が終わるまで案件は DONE になりません。改訂が終わると新しいリリース候補（Artifact）が作られます。古い候補はそのまま残ります（無効の印は付きません）。

### 13.5 議事録と監査イベント

議事録は DB ではなくファイルに保存します。

```text
<data_dir>/meetings/<project_id>_kickoff.md        # 既定 data_dir = <cwd>/.ai_company
<data_dir>/meetings/<project_id>_kickoff_heads.md  # 部長所見の追記（head_llm が有効なときだけ）
```

AuditEvent に追加した `event_type`:

| event_type | target | metadata の主な項目 |
|---|---|---|
| `executive_meeting_held` | project | attendees, absentees, assignment_count, work_orders, deferred_orders, open_questions, minutes_path |
| `meeting_work_order_issued` | task | key, meeting, department_id, section_id, role_required, depends_on |
| `meeting_work_order_deferred` | project | key, meeting, department_id, reason |
| `head_brief_requested` | task | meeting, department_id, head_role, held_orders |
| `head_brief_skipped` | project | meeting, department_id, reason（例: `unknown_or_empty_role_profile:<head>`） |
| `head_brief_applied` | task | department_id, issued, keys, rejected, confidence |
| `head_brief_fallback` | task | department_id, reason, mode, issued |
| `meeting_question_resolved` | project | number, question, answer, question_key, department_id |
| `president_decision_applied` | project | key, department_id, revision_task_ids, skipped |
| `project_reopened` | project | reason（`president_decision_revisions`）, decision_key |

既存のイベントに加わった metadata：

| event_type | 追加した項目 |
|---|---|
| `autonomous_run_finished` | `budget_wait_reason`（予算待ちで止まったときの理由。例: `hourly_max_runs`）、`release_candidate_ids`（この実行で作ったリリース候補すべて。`release_candidate_id` は従来どおり最後の1件）、`completed_project_ids` |
| `closure_loop_finished` | `coerced_roles`（監査が指定したが開発部の実装担当ではなかったため `coding_worker` に置き換えたロール。伏せ字化）、`dropped_proposals`（1回あたり5件の上限を超えて捨てた件数）、`risk_capped`（critical を high に下げた提案の番号） |
| `head_brief_fallback` | reason に `stalled_waiting:<status>:<n>cycles`（未着手のまま待機上限を超えた場合） |

自問監査からの起票（`closure_loops.apply_closure_result`）で `tasks` に入る値:

```text
kind                 implementation
role_required        coding_worker / backend_engineer / frontend_engineer（GAP_FIX_ROLES。それ以外は coding_worker）
risk_level           low / medium / high（critical は high に下げる。不明な値は medium）
title                1行化・伏せ字化・長さ制限
objective            改行は残し、制御文字と見えない文字を除いて伏せ字化（redact.multi_line）
acceptance_criteria  objective と同じ
```

- 監査タスク自身の `constraints.closure_summary` も、伏せ字化と `multi_line` を通してから2000字までで保存します。
| `scheduler_continue_finished` | `budget_wait`（`{reason, resume_at}`。resume_at は UTC の ISO 形式、不明なら null） |

### 13.6 未決事項（データモデル関連）

- 部門成果物は `constraints` の JSON に要約として残るだけで、Artifact（ファイル）にはなりません。ファイルとして残すには、書き込みを許す範囲を決める必要があります。
- `created_at` は SQLite では秒単位です。同じ秒の中では順序を判定できません。厳密な順序が必要な箇所には連番列が要ります（STEP7 のテスト修正で判明）。
- 部/課のマスタはコード（`org.py`）にあり、DB にはありません。組織を変更しても、既存行の id は自動では移行されません。
