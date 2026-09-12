# Task Queue and Scheduler

## Queueは中核

Task数とWorker数を分離する。

```text
Tasks = 500
Running Workers = 4
Queued = 496
```

## Task状態

```text
BACKLOG
WAITING
READY
RUNNING
REVIEW
DONE
FAILED
BLOCKED
CANCELLED
DEFERRED_BUDGET
```

## 基本遷移

```text
BACKLOG -> WAITING -> READY -> RUNNING -> REVIEW -> DONE
                           \-> FAILED -> READY(retry)
                                      \-> BLOCKED(retry limit)
```

## Priority

推奨:

```text
100 production incident
80 blocking bug
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
- file conflict risk
- resource lock

## WAITING / READY

依存未解決TaskはREADYへ入れない。

```text
Schema -> API -> Frontend -> E2E
```

## Scheduler責務

- capacity計算
- READY Task取得
- role適合
- Task lease取得
- Cost Governor照会
- Worker起動要求
- heartbeat監視
- 終了処理

## Concurrency設定例

```yaml
scheduler:
  max_concurrent_workers: 4
  max_workers_per_project: 3
  max_workers_per_role:
    backend: 2
    frontend: 2
    qa: 1
```

## Task Lease

二重実行防止。

```text
leased_by
lease_expires_at
```

Scheduler crash後も同一Taskの重複起動を防ぐ。
