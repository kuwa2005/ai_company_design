# Cursor Worker Lifecycle

## Worker状態

```text
HIRED -> STARTING -> ACTIVE -> REPORTING -> TERMINATING -> TERMINATED
```

異常:

```text
FAILED
TIMED_OUT
KILLED
```

## 原則

WorkerはTaskまたはTask groupのためだけに短期起動し、完了後terminateする。

## Worker Profile

```yaml
id: WORKER-000042
role: backend_engineer
project_id: PROJECT-0001
task_id: TASK-000127
runner: cursor
permission_profile: coding_worker
```

## Cursor Runner

Cursor CLI仕様は実装時点の公式仕様を確認し、config化する。
CLI名/flagsを業務ロジックへハードコードしない。

## Workerへの入力

- Role
- Objective
- Context
- Constraints
- Allowed Paths
- Forbidden Paths
- Acceptance Criteria
- Expected Output

## Structured Result例

```json
{
  "status": "completed",
  "changed_files": ["src/attendance/api.py"],
  "tests": {"passed": 42, "failed": 0},
  "commit": "abc123",
  "issues": []
}
```

Structured Resultが壊れている場合は正常完了とみなさない。

## Timeout

```yaml
worker:
  heartbeat_seconds: 30
  idle_timeout_minutes: 10
  hard_timeout_minutes: 45
```

## Zombie Worker

hard timeout時は段階的にterminateする。
OS差異はProcessControllerで抽象化。

## Memory

Worker sessionだけを会社の記憶にしない。
DB/Artifacts/Decision Log/Auditが正本。


## 10. Worker class

```text
implementation_worker
validation_worker
judge_worker
```

Validation Workerは原則read-onlyで開始し、コード変更を行わない。
問題を発見した場合は修正内容を報告し、Repair Taskを別途Queueへ登録する。

Judge Workerもコード変更禁止。
複数Validation Resultを統合して判定のみ行う。

## 11. 独立検証

Coding Workerが終了したあと、同じWorker sessionをReviewerとして再利用しない。
少なくとも別Executionとして起動し、将来は別モデル・別providerによるクロスレビューにも対応できる設計とする。
