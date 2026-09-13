# Task Queue and Scheduler

## 1. Queueは中核

Task数とWorker数を分離する。

```text
Tasks = 500
Running Workers = 4
Queued = 496
```

実装Taskだけでなく、Validation / Judgeも同じQueue統制下に置く。

## 2. Task kind

```text
implementation
validation_test
validation_spec
validation_security
validation_edge_case
validation_architecture
judge
repair
```

Task kindによって実行roleと権限を制限する。

## 3. Task状態

```text
BACKLOG
WAITING
READY
RUNNING
REVIEW_REQUIRED
VALIDATING
JUDGING
DONE
FAILED
BLOCKED
CANCELLED
DEFERRED_BUDGET
HUMAN_ESCALATION
```

## 4. 基本遷移

```text
BACKLOG -> WAITING -> READY -> RUNNING -> REVIEW_REQUIRED
                                             |
                                             v
                                         VALIDATING
                                             |
                                             v
                                          JUDGING
                                         /       \
                                      PASS       REJECT
                                       |            |
                                       v            v
                                      DONE      REPAIR -> READY
```

Coding Workerの成功だけではDONEへ遷移不可。

## 5. Priority

推奨:

```text
100 production incident
90 security blocker
80 blocking bug
70 required validation
60 current project
40 improvement
20 research
10 nice-to-have
```

ただし選定条件はPriorityだけではない。

- dependencies
- budget
- role
- project concurrency
- validation concurrency
- file conflict risk
- resource lock
- reviewer independence

## 6. WAITING / READY

依存未解決TaskはREADYへ入れない。

```text
Implementation -> Test Review -> Judge
Implementation -> Security Review -> Judge
```

Judgeはrequired reviewがすべて揃うまでWAITING。

## 7. Scheduler責務

- capacity計算
- READY Task取得
- role適合
- Task lease取得
- Cost Governor照会
- Worker起動要求
- heartbeat監視
- 終了処理
- reviewer independence確認
- validation quorum確認

## 8. Concurrency設定例

```yaml
scheduler:
  max_concurrent_workers: 4
  max_workers_per_project: 3
  max_workers_per_role:
    backend: 2
    frontend: 2
    qa: 1
    security_reviewer: 1
    judge: 1

  max_validation_workers: 2
```

`max_validation_workers`も`max_concurrent_workers`の内数とする。
検証だけ別枠で無制限起動してはいけない。

## 9. Task Lease

二重実行防止。

```text
leased_by
lease_expires_at
```

Scheduler crash後も同一Taskの重複起動を防ぐ。

## 10. Validation fan-out

リスクに応じてReviewer数を変える。

```text
low    -> 1 reviewer + judge
medium -> 2-3 specialist reviewers + judge
high   -> test + spec + security + edge-case + judge
critical -> high構成 + architecture + human escalation候補
```

Reviewer数を「多ければ多いほど良い」とはしない。
Cost Governorが上限を決める。

## 11. Self-review禁止

以下をScheduler / Policyで拒否する。

```text
implementation.execution_id == validation.execution_id
```

同一Worker identity / 同一実行コンテキストによる最終レビューも拒否する。
