# AI Company / Cursor Workforce Orchestrator

人間は1つの常勤AIだけを操作し、そのAIが必要な時だけCursor Coding Agentを短期雇用するための設計一式。

## Core model

```text
Human Owner
    |
    v
Secretary / Chief of Staff Cursor (persistent)
    |
    v
Task Graph / Backlog
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
Ephemeral Cursor Workers (0..N)
```

## 最重要原則

> Agentは直接Agentを起動してはならない。すべてのWorker起動要求はQueueとSchedulerを経由する。

> SchedulerはCost Governorの許可なしにCursor Workerを起動してはならない。

## 読む順番

1. `00_START_HERE.md`
2. `01_PRODUCT_VISION.md`
3. `02_SYSTEM_ARCHITECTURE.md`
4. `03_SECRETARY_AGENT.md`
5. `04_TASK_QUEUE_AND_SCHEDULER.md`
6. `05_COST_GOVERNOR.md`
7. `06_CURSOR_WORKER_LIFECYCLE.md`
8. `07_DATA_MODEL.md`
9. `08_PERMISSION_APPROVAL_AUDIT.md`
10. `09_CLI_AND_USER_EXPERIENCE.md`
11. `10_IMPLEMENTATION_ROADMAP.md`
12. `11_MVP_ACCEPTANCE_TESTS.md`
13. `12_SECURITY_AND_FAILURE_MODES.md`
14. `13_FUTURE_EXTENSIONS.md`
15. `config/`
16. `prompts/`
