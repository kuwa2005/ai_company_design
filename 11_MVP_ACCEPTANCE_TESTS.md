# MVP Acceptance Tests

## Test 1: Queue
Taskを作成するとREADYへ入る。

## Test 2: Dependency
依存Task未完了ならWAITING。依存Task完了後READY。

## Test 3: Concurrency
`max_concurrent_workers=2`時、READY Taskが10件あってもRUNNINGは最大2。

## Test 4: Hard Budget
hard limit到達時、READY Taskが存在してもWorkerを起動しない。

## Test 5: Soft Budget
soft threshold超過時、設定に従いmax worker数が減る。

## Test 6: Retry
失敗Taskはmax_retriesまでは再投入。超過後BLOCKED。

## Test 7: Circuit Breaker
短時間にfailure thresholdへ達したら新規起動停止。

## Test 8: Timeout
Workerがhard timeout超過時にterminateされる。

## Test 9: Audit
Task/Worker/Budgetの主要イベントがAuditへ記録される。

## Test 10: No Direct Spawn
Secretary serviceからWorker Managerを直接呼べない設計であること。
Staffing Request / Task経由のみ。

## Test 11: Budget Reservation
同時に複数Taskが起動判定されても、同じ残予算を重複使用しない。

## Test 12: Coding Worker Cannot Self-Approve
Coding WorkerのExecution ResultがsuccessでもTaskはDONEにならず`REVIEW_REQUIRED`となる。

## Test 13: Reviewer Independence
Coding Workerと同じExecution / Worker identityをReviewerへ割当しようとすると拒否される。

## Test 14: Validation Uses Queue
Reviewerを直接起動せずValidation TaskがREADY Queueを経由する。

## Test 15: Validation Budget
Reviewer 5体が要求されてもCost Governorの上限が2なら同時起動は最大2。

## Test 16: Judge Waits for Required Reviews
必須Reviewer結果が不足している場合Judge TaskはWAITINGのまま。

## Test 17: Judge Reject Creates Repair
Security ReviewerがFAILしJudgeがREJECTした場合Repair Taskが生成される。

## Test 18: Revalidation
Repair Task完了後、新しいValidation executionを作成し、古いPASSを使い回さない。

## Test 19: Human Escalation
Reviewer間の重大な矛盾をJudgeが解消できない場合`HUMAN_ESCALATION`となる。

## Test 20: End-to-End No-look MVP

```text
Implementation Task create
-> READY
-> Coding Worker
-> REVIEW_REQUIRED
-> Test Reviewer
-> Security Reviewer
-> Judge
-> PASS
-> DONE
-> all Workers TERMINATED
-> Audit / Evidence persisted
```

がpytestで成功。
