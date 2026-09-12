# Implementation Roadmap

## Milestone 0 - Skeleton

- pyproject.toml
- package layout
- config loader
- DB bootstrap
- CLI skeleton

完了: `ai-company init` が成功。

## Milestone 1 - Queue Core

- Project / Task
- repositories
- status transition
- dependency
- WAITING / READY
- priority

完了: dependency解決でWAITING -> READY。

## Milestone 2 - Scheduler + Mock Worker

- Scheduler
- concurrency
- Worker model
- Worker Manager
- Mock Runner

完了: READYが10件でも上限数のみRUNNING。

## Milestone 3 - Cost Governor

- hourly/daily/monthly
- project/task limits
- soft/hard limits
- budget ledger
- reservation
- retry limits
- circuit breaker

完了: hard limit時にWorkerが絶対起動しない。

## Milestone 4 - Audit + Reliability

- audit events
- execution log
- lease
- timeout
- zombie cleanup

完了: Scheduler異常終了でもTask二重起動なし。

## Milestone 5 - Cursor Runner

- CLI adapter
- prompt builder
- stdout/stderr
- timeout
- structured output parser

完了: 実Cursorへ1 Taskを渡し、結果をDB保存。

## Milestone 6 - Secretary v1

- Goal
- Task Proposal
- Staffing Request
- Human Summary

重要: Secretaryにspawn権限を与えない。

## Milestone 7 - Review / Retry

- Reviewer Worker
- QA Worker
- retry
- BLOCKED escalation

## Milestone 8 - Dynamic Staffing

- required_role
- per-role capacity
- safe parallelism
- conflict detection

## Milestone 9 - Product-level Autonomous Flow

```text
Goal
-> PRD
-> Architecture
-> Task Graph
-> Staffing
-> Workers
-> Review
-> Test
-> Human Summary
```
