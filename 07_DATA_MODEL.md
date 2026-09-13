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
```

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
