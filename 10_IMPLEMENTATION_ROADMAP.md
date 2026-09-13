# Implementation Roadmap

本ロードマップは「ノールック開発」の要件を反映したv0.2である。

## Milestone 0 - Skeleton

- pyproject.toml
- package layout
- config loader
- DB bootstrap
- CLI skeleton
- Task kind / Worker class enumの基礎

完了: `ai-company init` が成功。

### ノールック開発による影響
小。後続で実装TaskとValidation Taskを区別できる型だけ先に用意する。

---

## Milestone 1 - Queue Core

- Project / Task
- repositories
- status transition
- dependency
- WAITING / READY
- priority
- Task kind (`implementation`, `validation`, `judge`, `repair`)
- Validation依存関係

完了:
- dependency解決でWAITING -> READY
- Judge Taskはrequired Validation Resultが揃うまでREADYにならない

### ノールック開発による影響
**大。** 検証処理を特別な直呼びにせず、すべて通常Queueへ載せる必要がある。

---

## Milestone 2 - Scheduler + Mock Worker

- Scheduler
- concurrency
- Worker model
- Worker Manager
- Mock Runner
- role別slot
- reviewer independence check
- max_validation_workers

完了:
- READYが10件でも上限数のみRUNNING
- Coding Workerと同一ExecutionをReviewerに割当できない

### ノールック開発による影響
**大。** Reviewer多重起動もSchedulerが統制しなければ、品質強化がそのまま課金暴走になる。

---

## Milestone 3 - Cost Governor

- hourly/daily/monthly
- project/task limits
- soft/hard limits
- budget ledger
- reservation
- retry limits
- circuit breaker
- validation budget
- max reviewers per task
- risk-based validation fan-out

完了:
- hard limit時にWorkerが絶対起動しない
- Validationも予算外で勝手に起動しない

### ノールック開発による影響
**大。** 複数検証AIのコストを明示的に管理する必要がある。

---

## Milestone 4 - Audit + Reliability

- audit events
- execution log
- lease
- timeout
- zombie cleanup
- Validation provenance
- Evidence保存
- Judge decision保存

完了:
- Scheduler異常終了でもTask二重起動なし
- 「誰が実装し、誰が何を検証し、なぜPASSしたか」を追跡可能

### ノールック開発による影響
**中〜大。** 人間がコードを見ない分、後から判断根拠を再現できることが重要。

---

## Milestone 5 - Cursor Runner

- CLI adapter
- prompt builder
- stdout/stderr
- timeout
- structured output parser
- role prompt profiles
- reviewer structured result parser
- judge structured result parser

完了:
- 実CursorへImplementation Taskを渡し結果をDB保存
- 実CursorへReview Taskを渡しPASS/FAIL + evidenceをDB保存

### ノールック開発による影響
**大。** Coding用Runnerだけではなく、専門ReviewerとJudgeを同じRunner基盤で安全に起動できる必要がある。

---

## Milestone 6 - Secretary v1

- Goal
- Task Proposal
- Staffing Request
- Human Summary
- Validation Request proposal
- risk classification

重要: Secretaryにspawn権限を与えない。

完了:
- Coding Workerの自己申告をDONEとせずValidationへ送れる

### ノールック開発による影響
**中。** Secretaryは品質判定者ではなく、検証を手配し例外を人間へ上げる役にする。

---

## Milestone 7 - Independent Validation Core

旧「Review / Retry」を全面改訂する。

実装:
- Validation Plan
- Test Reviewer
- Spec Reviewer
- Security Reviewer
- Edge-case / Error-path Reviewer
- Architecture Reviewer（高リスク時）
- Evidence schema
- PASS / FAIL / UNCERTAIN
- Reviewer isolation

完了:
- 1つの実装Taskから複数独立Validation Taskを生成可能
- Coding Worker自身の自己評価なしで検証可能

### ノールック開発による影響
**最大。** ここがノールック開発成立の中心。

---

## Milestone 8 - Judge + Repair Loop

実装:
- Judge Worker
- reviewer result aggregation
- disagreement detection
- PASS / REJECT / HUMAN_ESCALATION
- Repair Task生成
- retry / revalidation
- infinite loop guard

完了:

```text
Implementation
-> Multi Review
-> Judge
-> REJECT
-> Repair
-> Re-validation
-> PASS
```

が人間介入なしで1回以上完走する。

### ノールック開発による影響
**最大。** 複数AIの意見を並べるだけでなく、最終判定と差し戻しを自動化する。

---

## Milestone 9 - Dynamic Staffing

- required_role
- per-role capacity
- safe parallelism
- conflict detection
- risk-based reviewer staffing
- specialist reviewer on-demand hiring

完了:
- 必要な時だけCoding / Review / Judge Workerを採用し終了できる

### ノールック開発による影響
**大。** 検証専門職も短期雇用の対象になる。

---

## Milestone 10 - Product-level Autonomous / No-look Flow

```text
Goal
-> PRD
-> Architecture
-> Task Graph
-> Staffing
-> Coding Workers
-> Independent Validation
-> Judge
-> Repair Loop as needed
-> Release Candidate
-> Human Summary / Exception only
```

完了条件:

人間が各Taskを開かなくても、通常ケースではプロジェクトが進み、1日数回の要約と例外通知だけで状況を把握できる。

人間に上げるもの:
- business decision
- unresolved reviewer disagreement
- critical security finding
- hard budget override
- policy override
- production release approval（初期運用）
