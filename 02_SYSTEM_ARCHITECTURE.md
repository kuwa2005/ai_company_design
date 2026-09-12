# System Architecture

## 全体

```text
Human Owner
   |
   v
Secretary Agent
   |
   v
Goal / Project
   |
   v
Task Graph
   |
   +--> WAITING (dependency)
   |
   +--> READY -> Priority Queue -> Scheduler -> Cost Governor -> Worker Manager -> Cursor Worker
                                                                       |
                                                                       v
                                                                  REVIEW / DONE
```

## コンポーネント

- Secretary Agent: 人間との唯一の常設窓口
- Task Graph: 依存関係を含む仕事グラフ
- Priority Queue: 実行可能Taskの待ち行列
- Scheduler: 空きslotへTask割当
- Cost Governor: 同時数/時間/日/月/案件/Task制限
- Worker Manager: process lifecycle
- Cursor Runner: Cursor CLI Adapter
- Audit Engine: 重要操作記録

## 強制実行経路

```text
LLM Proposal
-> Policy Check
-> Permission Check
-> Queue
-> Scheduler
-> Cost Check
-> Execution
-> Audit
```

## 直接起動禁止

```text
Secretary -> Cursor    # 禁止
Worker -> Cursor       # 禁止
Reviewer -> Cursor     # 禁止
```

必ずTaskまたはStaffing Requestとして登録する。

## Workerは使い捨て

```text
spawn -> execute -> report -> terminate
```

長期記憶はDB/Artifact/Decision Log/Auditへ保存する。
