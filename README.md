# AI Company / Cursor Workforce Orchestrator

人間は1つの常勤AIだけを操作し、その常勤AIが必要に応じて複数のCursor Coding Agentを短期雇用・指揮・終了するための基盤です。

## まず読む（利用者向け）

| 文書 | 内容 |
|------|------|
| **[概要.md](./概要.md)** | 何の製品か・設計の核 |
| **[なにができる.md](./なにができる.md)** | 機能一覧とできないこと |
| **[使用説明書.md](./使用説明書.md)** | インストール〜日常操作〜例外対応 |
| [OPERATING_FLOW.md](./OPERATING_FLOW.md) | 人のコマンドと機械が回す範囲（1枚） |

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

## ローカル開発での使い方

```bash
# 1) このリポジトリで CLI を PATH に通す（一度だけ）
cd ~/workspace/ai_company_design
bash scripts/install-local.sh

# 2) 作業用プロジェクトディレクトリで起動（状態は cwd/.ai_company）
mkdir -p ~/workspace/web勤怠 && cd ~/workspace/web勤怠
ai-company start "優秀なWeb勤怠を作って" --autonomous --runner mock
```

`~/.local/bin` が PATH に無い場合:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

詳しい手順・例外対応は **[使用説明書.md](./使用説明書.md)** を参照してください。

将来は GitHub からのワンライン導入（pipx / uvx）を想定しています。いまは editable + shim です。

予算デフォルトは **$0（Cursor 月額プラン内想定）** です。追加USD枠は設けず、暴走防止は `max_runs` / 同時Worker上限で行います。

将来マイルストーン: **M11 Local Multi-Project Web UI**（ローカル複数案件、CLI 必須運用からの脱却。詳細は `10_IMPLEMENTATION_ROADMAP.md`）。

## ZIP内の読む順番

### 利用者

1. `概要.md`
2. `なにができる.md`
3. `使用説明書.md`
4. `OPERATING_FLOW.md`

### 設計・実装者

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
