# AI Company / Cursor Workforce Orchestrator

人間は1つの常勤AIだけを操作し、その常勤AIが必要に応じて複数のCursor Coding Agentを短期雇用・指揮・終了するための基盤設計です。

## コンセプト

```text
Human Owner
    |
    v
Secretary / Chief of Staff Cursor
    |
    v
Task Graph / Backlog
    |
    v
Priority Queue
    |
    v
Cost Governor
    |
    v
Scheduler
    |
    v
Ephemeral Cursor Workers
    |
    v
Independent Validation Workers
    |
    v
Judge
```

常勤するAIは原則1体です。

- Secretary / Chief of Staff: 常勤
- 実装Worker Cursor: 必要な時だけ起動
- 検証Worker Cursor: 実装完了時だけ必要数を起動
- Judge Cursor: 複数検証結果を統合するときだけ起動
- Worker終了後: terminate
- Agentが直接Agentを起動することは禁止
- 全Worker起動はQueue -> Scheduler -> Cost Governorを通す

## ノールック開発の定義

本プロジェクトにおける「ノールック開発」は、人間が何も検証しないことではありません。

> 人間による逐次コードレビューを必須にせず、実装者とは独立した複数のAI検証系とJudgeによって品質ゲートを構成し、人間は例外・重要判断・最終承認に集中する方式。

Coding Worker自身の「テスト成功」「完成した」という自己申告だけでDONEにしてはいけません。

## 最重要原則

> Agentは直接Agentを起動してはならない。
> すべてのAgent起動要求はQueueとSchedulerを経由する。

> SchedulerはCost Governorの許可なしにCursor Workerを起動してはならない。

> 実装Workerは自分自身の成果を最終承認してはならない。

> DONEへ遷移するには、定義されたValidation Planを満たしJudgeがPASSすること。

## ZIP内の読む順番

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
14. `14_NO_LOOK_DEVELOPMENT.md`
15. `15_VALIDATION_ORCHESTRATION.md`
16. `16_MILESTONE_IMPACT_ANALYSIS.md`
17. `13_FUTURE_EXTENSIONS.md`
18. `config/`
19. `prompts/`
