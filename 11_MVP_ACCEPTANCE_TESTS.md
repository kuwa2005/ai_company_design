# MVP Acceptance Tests

## 1 Queue
Task作成でREADY。

## 2 Dependency
依存未完了ならWAITING。完了後READY。

## 3 Concurrency
max=2ならREADYが10件でもRUNNING<=2。

## 4 Hard Budget
hard limit到達時はWorker起動0。

## 5 Soft Budget
usage thresholdに応じてmax workerが低下。

## 6 Retry
max_retries超過でBLOCKED。

## 7 Circuit Breaker
failure threshold到達で新規起動停止。

## 8 Timeout
hard timeoutでWorker terminate。

## 9 Audit
主要イベントが全て記録。

## 10 No Direct Spawn
SecretaryからWorker processを直接spawnできない設計。

## 11 Budget Reservation
同時Taskが同一残予算を二重使用しない。

## 12 E2E

```text
Task create
-> READY
-> Scheduler
-> Cost ALLOW
-> Worker HIRED
-> Mock Runner
-> DONE
-> Worker TERMINATED
-> Audit
```

がpytestで成功。
