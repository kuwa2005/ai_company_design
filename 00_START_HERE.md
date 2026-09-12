# START HERE - Coding Agent向け実装指示

## 1. あなたの役割

あなたはこのリポジトリの最初のCoding Agentです。

最初から完全自律のAI会社を作らないでください。
最初に作るものは、**AIが暴走せず、必要なCursor Workerだけを順番に安全に起動できる実行基盤**です。

## 2. 最終ゴール

最終的には人間が次のような指示だけを出せる状態を目指します。

```bash
ai-company start "優秀なWeb勤怠を作って"
```

するとSecretaryが以下を行います。

```text
Goal理解
-> Task分解
-> 必要職種/人数を提案
-> Staffing Request
-> Queue
-> Scheduler
-> Cost Governor
-> Cursor Worker起動
-> 成果回収
-> Review / Retry / Escalation
-> Worker terminate
-> Humanへ要約
```

## 3. 第1マイルストーン

まずCLIで以下だけを完成させてください。

```text
Task手動作成
-> READY Queue
-> Scheduler
-> Cost Governor
-> Mock Worker
-> 実行結果保存
-> DONE
-> Worker TERMINATED
-> Audit記録
```

## 4. 実装順序

### Phase 1
- Pythonプロジェクト初期化
- Pydanticモデル
- YAML config loader
- SQLite
- Project / Task / Worker model

### Phase 2
- Task Queue
- Priority
- WAITING / READY
- Dependency判定
- Task状態遷移

### Phase 3
- Scheduler
- max_concurrent_workers
- Worker slot管理
- Mock Runner

### Phase 4
- Cost Governor
- hourly/daily/monthly制限
- soft/hard limit
- budget reservation
- retry上限
- circuit breaker

### Phase 5
- Audit Log
- CLI
- E2E pytest

### Phase 6
- Cursor CLI Runner
- timeout
- stdout/stderr capture
- structured result parsing

### Phase 7
- Secretary v1
- Staffing Request
- Task Proposal
- Human Summary

## 5. 今回作らないもの

- Web UI
- Kubernetes
- 100 Worker同時実行
- 完全自律経営
- 高度なRAG
- 複雑な固定組織階層
- 本番自動デプロイ

## 6. 絶対禁止

SecretaryからCursorを直接起動してはいけません。

必ず:

```text
Secretary
-> Task / Staffing Request
-> Queue
-> Scheduler
-> Cost Governor
-> Worker Manager
-> Cursor Runner
```

を通すこと。

## 7. 推奨技術

```text
Python 3.12+
Typer
Pydantic v2
SQLAlchemy 2.x
SQLite
PyYAML
pytest
ruff
mypy
asyncio
```

## 8. MVP完了条件

以下が動作すること。

```bash
ai-company init
ai-company task create --title "sample" --priority 50
ai-company task list
ai-company scheduler run-once
ai-company worker list
ai-company budget status
ai-company audit list
```

pytestで次が1本通ること。

```text
Task create
-> READY
-> Scheduler取得
-> Budget ALLOW
-> Worker HIRED
-> Mock run
-> DONE
-> Worker TERMINATED
-> Audit recorded
```

## 9. 判断原則

曖昧なら安全側。

- 起動しない
- fail closed
- Auditへ理由を残す
- 制限を勝手に緩めない

## 10. ゴール定義

第1マイルストーンのゴールは「賢いAI」ではない。

**安全なJob Queue + Scheduler + Cost Guard + Worker Lifecycleが壊れずに動くこと。**
