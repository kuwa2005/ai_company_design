# Permission, Approval and Audit

## 強制順序

```text
Proposal
-> Policy
-> Permission
-> Cost
-> Approval
-> Execution
-> Audit
```

## Permission例

```yaml
profiles:
  human_owner:
    permissions: ["*"]
  secretary:
    permissions:
      - task.create
      - task.prioritize
      - staffing.request
      - project.report
  coding_worker:
    permissions:
      - code.edit
      - code.commit
      - test.execute
      - shell.safe
  reviewer:
    permissions:
      - code.read
      - test.execute
      - task.review
```

## Human Approval候補

- production deploy
- customer data access
- secret modification
- destructive operation
- external paid service
- hard budget override
- policy modification
- main/master direct merge

## Audit Events

```text
task_created
task_ready
task_started
task_completed
task_failed
task_blocked
staffing_requested
worker_hired
worker_started
worker_terminated
budget_reserved
budget_spent
budget_denied
approval_requested
approval_approved
approval_rejected
circuit_opened
permission_denied
```

Auditは通常ログと分離し、JSON Lines等で追跡可能にする。


## 6. Validation権限分離

```yaml
profiles:
  validation_reviewer:
    permissions:
      - code.read
      - test.execute
      - validation.report

  security_reviewer:
    permissions:
      - code.read
      - test.execute
      - security.scan.safe
      - validation.report

  judge:
    permissions:
      - validation.read
      - judge.decide
```

ReviewerとJudgeへ`code.edit`をデフォルト付与しない。

Auditには以下も記録する。

```text
validation_plan_created
validation_started
validation_passed
validation_failed
validation_uncertain
judge_started
judge_passed
judge_rejected
judge_escalated
repair_task_created
```
