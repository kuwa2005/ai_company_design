# START HERE - Coding Agent向け実装指示

## 1. あなたの役割

あなたはこのリポジトリの最初のCoding Agentです。

最初から完全自律のAI会社を作らないでください。
最初に作るものは、**AIが暴走せず、必要なCursor Workerだけを順番に安全に起動でき、実装者とは独立した検証Workerで成果を判定できる実行基盤**です。

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
-> Cursor Coding Worker起動
-> 成果回収
-> 独立Validation Plan生成
-> Test / Spec / Security / Edge-case / Architecture Reviewerを必要数だけ起動
-> Judgeが結果を統合
-> PASSならDONE
-> REJECTなら修正TaskをQueueへ戻す
-> Worker terminate
-> Humanへ要約
```

## 3. ノールック開発について

本設計では、人間がコードを逐次見ない運用を将来目標とする。
ただし「実装AIを信用してそのまま採用する」方式は禁止。

想定するAIの失敗:

- それらしい誤実装
- テストそのものの誤り
- 仕様の穴の都合のよい解釈
- セキュリティ上危険な近道
- エラー処理・例外条件の欠落

これらを人間の属人的レビューだけに依存させず、独立した専門Validation Workerで検出する。

## 4. 第1マイルストーン

まずCLIで以下を完成させてください。

```text
Task手動作成
-> READY Queue
-> Scheduler
-> Cost Governor
-> Mock Coding Worker
-> 実行結果保存
-> REVIEW_REQUIRED
-> Mock Reviewer
-> Mock Judge
-> DONE
-> 全Worker TERMINATED
-> Audit記録
```

MVP段階でも「Coding Workerの成功だけでDONE」は禁止する。
最低1つの独立Reviewer + Judge経由を実装する。

## 5. 実装順序

### Phase 1
- Pythonプロジェクト初期化
- Pydanticモデル
- YAML config loader
- SQLite
- Project / Task / Worker model
- Task kind (`implementation`, `validation`, `judge`)

### Phase 2
- Task Queue
- Priority
- WAITING / READY
- Dependency判定
- Task状態遷移
- Validation Taskも同一Queueへ載せる

### Phase 3
- Scheduler
- max_concurrent_workers
- role別slot管理
- Mock Runner
- 実装者とReviewerの独立性チェック

### Phase 4
- Cost Governor
- hourly/daily/monthly制限
- soft/hard limit
- budget reservation
- retry上限
- validation fan-out上限
- circuit breaker

### Phase 5
- Audit Log
- CLI
- E2E pytest
- Validation provenance記録

### Phase 6
- Cursor CLI Runner
- timeout
- stdout/stderr capture
- structured result parsing
- role別prompt profile

### Phase 7
- Secretary v1
- Staffing Request
- Task Proposal
- Human Summary
- Validation Plan proposal

### Phase 8
- Independent Validation
- Test Reviewer
- Spec Reviewer
- Security Reviewer
- Edge-case Reviewer
- Architecture Reviewer
- Judge
- PASS / REJECT gate

## 6. 今回作らないもの

- Web UI
- Kubernetes
- 100 Worker同時実行
- 完全自律経営
- 高度なRAG
- 複雑な固定組織階層
- 本番自動デプロイ
- 無制限なReviewer多重起動

## 7. 絶対禁止

SecretaryやWorkerからCursorを直接起動してはいけません。

必ず:

```text
Secretary / Worker Result
-> Task / Staffing Request / Validation Request
-> Queue
-> Scheduler
-> Cost Governor
-> Worker Manager
-> Cursor Runner
```

を通すこと。

さらに:

- Coding Workerは自分のTaskを最終PASSにできない
- Reviewerは原則コードを書き換えない
- Judgeはコードを書き換えない
- Validation不足状態でDONEにしない
- Cost制限をValidationだけ例外扱いしない

## 8. 推奨技術

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

## 9. MVP完了条件

以下が動作すること。

```bash
ai-company init
ai-company task create --title "sample" --priority 50
ai-company task list
ai-company scheduler run-once
ai-company worker list
ai-company validation status TASK-000001
ai-company budget status
ai-company audit list
```

pytestで次が1本通ること。

```text
Implementation Task create
-> READY
-> Scheduler取得
-> Budget ALLOW
-> Coding Worker HIRED
-> Mock implementation
-> REVIEW_REQUIRED
-> Validation Task create
-> Reviewer Worker HIRED
-> Review result
-> Judge Worker HIRED
-> Judge PASS
-> DONE
-> Workers TERMINATED
-> Audit recorded
```

## 10. 判断原則

曖昧なら安全側。

- 起動しない
- fail closed
- Auditへ理由を残す
- 制限を勝手に緩めない
- 実装者の自己申告を最終根拠にしない

## 11. ゴール定義

第1マイルストーンのゴールは「賢いAI」ではない。

**安全なJob Queue + Scheduler + Cost Guard + Worker Lifecycle + 独立Validation Gateが壊れずに動くこと。**
