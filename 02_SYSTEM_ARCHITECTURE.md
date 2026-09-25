# System Architecture

## 1. 全体構成

```text
                     Human Owner
                         |
                         v
                Secretary Agent
                         |
                         v
                 Goal / Project
                         |
                         v
                   Task Graph
                         |
             +-----------+-----------+
             |                       |
             v                       v
          WAITING                  READY
                                     |
                                     v
                              Priority Queue
                                     |
                                     v
                               Scheduler
                                     |
                                     v
                              Cost Governor
                                     |
                                     v
                              Worker Manager
                                     |
                                     v
                              Coding Worker
                                     |
                                     v
                            REVIEW_REQUIRED
                                     |
                     +---------------+---------------+
                     |       |       |       |       |
                     v       v       v       v       v
                   Test     Spec  Security  Edge   Architecture
                 Reviewer Reviewer Reviewer Reviewer Reviewer
                     |       |       |       |       |
                     +---------------+---------------+
                                     |
                                     v
                                  Judge
                              +------+------+
                              |             |
                              v             v
                            PASS          REJECT
                              |             |
                              v             v
                            DONE        Fix Task -> Queue

After implementation queue is empty (not yet release):

  Completeness Auditor  ("本当にもう無いか？")
           |
           +-- GAPS --> new Implementation tasks --> ...
           +-- CLEAR (repeat) --> Project Coordinator（プロジェクト推進）
                                      |
                                      +-- CLEAR --> Product Planner（商品企画）
                                                      |
                                                      +-- CLEAR --> Usability Auditor
                                                                      |
                                                                      +-- CLEAR --> Customer Success
                                                                                      |
                                                                                      +-- CLEAR --> IT Operations（情報システム）
                                                                                                      |
                                                                                                      +-- CLEAR --> Compliance（法務合规）
                                                                                                                      |
                                                                                                                      +-- CLEAR --> Test Auditor（テスト監査）
                                                                                                                                      |
                                                                                                                                      +-- CLEAR --> Quality Auditor（品質向上部）
                                                                                                                                                      |
                                                                                                                                                      +-- CLEAR --> Sales（営業・軽量）
                                                                                                                                                                      |
                                                                                                                                                                      +-- CLEAR --> Maintenance（保守部・軽量）
                                                                                                                                                                                      |
                                                                                                                                                                                      +-- CLEAR --> Release Candidate
```

## 2. コンポーネント

### Secretary Agent
人間との唯一の常設窓口。Goalと例外を管理する。

### Task Graph
実装TaskだけでなくValidation Task、Judge Taskも同じ依存グラフで管理する。

### Priority Queue
実行可能Taskの優先順位付き待ち行列。検証ジョブもQueue外で勝手に起動しない。

### Scheduler
空きWorker slotへTaskを割当。実装・検証・Judgeの役割制約を確認する。

### Cost Governor
全Cursor実行を共通予算で統制する。Validation fan-outも対象。

### Worker Manager
実際にCursor processを起動・停止する唯一のコンポーネント。

### Cursor Runner
Cursor CLIを実行するAdapter。

### Validation Orchestrator
Taskのリスクに応じてValidation Planを具体化し、専門Reviewer TaskをQueueへ登録する。
直接Workerを起動してはならない。

### Judge
複数Reviewerの証拠を統合し、PASS / REJECT / HUMAN_ESCALATIONを判定する。
Judge自身はコードを書かない。

### Audit Engine
重要操作、レビュー根拠、Judge判定を記録する。

### Supervisor Chain

技術的な失敗は人間へ直行させず、`roles.py` の `reports_to` を1段ずつ辿る。

```text
Coding Worker
  -> Engineering Manager
     -> Technical Director
        -> Human Owner（AIの権限外、または全AI階層を使い切った場合のみ）
```

各上司は `RETRY / REASSIGN / ESCALATE` の構造化判断を返す。判断から生じる
修正作業も必ずTask Queueへ登録し、上司がWorkerを直接起動することはない。

## 3. 重要な境界

LLMは提案・分析するだけ。

```text
LLM Proposal
   |
   v
Policy Check
   |
   v
Permission Check
   |
   v
Cost Check
   |
   v
Scheduler
   |
   v
Execution
   |
   v
Independent Validation
   |
   v
Judge
```

## 4. 直接起動禁止

以下は禁止。

```text
Secretary -> Cursor
Coding Worker -> Reviewer Cursor
Reviewer -> Judge Cursor
Judge -> Fix Cursor
```

必ず:

```text
Agent result
-> Task / Validation Request
-> Queue
-> Scheduler
-> Cost Governor
-> Worker Manager
```

とする。

## 5. 独立性

最低限以下を保証する。

- 実装WorkerとReviewerは別Execution
- Reviewerは実装Workerの自己評価を鵜呑みにしない
- Security Reviewerは実装者のテスト成功だけでPASSしない
- Judgeは独立Reviewerの結果とEvidenceを読む
- ReviewerのFAILをCoding Workerが勝手に無効化できない

将来は異なるモデル / providerによるクロスレビューも可能にする。

## 6. プロセスモデル

Workerは原則使い捨て。

```text
spawn
-> execute assigned role
-> structured report + evidence
-> terminate
```

長期記憶はWorker sessionへ依存しない。
保存先:

- DB
- Task artifact
- Validation result
- Judge decision
- Decision log
- PRD
- Architecture
- Audit log

## 7. Cursor Agent CLI実行モード

- 実装Worker: `--auto-review --sandbox enabled`
- Reviewer / Judge: `--mode ask --sandbox enabled`
- Engineering Manager / Technical Director: `--mode plan --sandbox enabled`
- `--force` は使用しない

これにより、安全と分類された実装操作は非対話で進めつつ、検証者と上司は
workspaceを変更しない。
