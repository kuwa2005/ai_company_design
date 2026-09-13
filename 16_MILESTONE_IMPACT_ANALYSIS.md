# Milestone Impact Analysis - ノールック開発反映

## 1. 概要

「ノールック開発は正しいか」で整理した内容は、旧Milestone 7のReviewer追加だけでは不十分。
独立検証を実行するには、Queue / Scheduler / Cost / Runner / Auditまで前段から設計変更が必要。

## 2. 影響マトリクス

| Milestone | 影響度 | 主な変更 |
|---|---|---|
| M0 Skeleton | 小 | Task kind / Worker classの型を追加 |
| M1 Queue Core | 大 | Validation / Judge / RepairをTaskとしてQueue化 |
| M2 Scheduler | 大 | reviewer独立性、role slot、validation concurrency |
| M3 Cost Governor | 大 | 検証fan-out、reviewer上限、validation予算 |
| M4 Audit + Reliability | 中〜大 | Evidence、review provenance、Judge根拠保存 |
| M5 Cursor Runner | 大 | Coding以外にReviewer / Judge profileを実行 |
| M6 Secretary v1 | 中 | 自己申告完了を禁止、Validation Request生成 |
| M7 Independent Validation | 最大 | 専門Reviewer群を実装 |
| M8 Judge + Repair Loop | 最大 | 総合判定・差し戻し・再検証 |
| M9 Dynamic Staffing | 大 | 検証専門職もオンデマンド採用 |
| M10 Product Autonomous Flow | 最大 | 人間のTask逐次レビューなしで通常系完走 |
| M11 Local Multi-Project Web UI | 大 | ローカル複数案件の操作面（CLI不要化）。公開SaaSではない |

## 3. なぜM1から影響するのか

ReviewerをMilestone 7で単純に追加し、直接Cursorを呼ぶと以下が起きる。

```text
Coding Task finished
-> reviewer x 5 direct spawn
-> Scheduler bypass
-> Cost Governor bypass
-> 課金暴走
```

したがってValidation Taskも最初からQueueの第一級Taskとして扱う。

## 4. なぜM2/M3が重要か

ノールック品質を上げようとしてReviewer数を増やすほど、多重起動リスクが増える。

よって:

```text
Quality Control
!= Unlimited Agents
```

とする。

SchedulerとCost Governorが品質戦略より上位の強制境界になる。

## 5. M7の変更

旧M7:

```text
Reviewer Worker
QA Worker
retry
```

では弱い。

新M7:

```text
Validation Plan
Test Reviewer
Spec Reviewer
Security Reviewer
Edge-case Reviewer
Architecture Reviewer
Evidence
Independent execution
```

へ拡張する。

## 6. M8を新設する理由

複数Reviewerを起動して終わりでは、人間が複数結果を読むことになり「1つのAgentだけを見る」という目標に反する。

そのためJudge + Repair Loopを独立Milestoneとする。

## 7. M10の完成条件

次が通常系で成立すること。

```text
Human Goal
-> Secretary
-> Coding Workers
-> Independent Validation
-> Judge
-> Automatic Repair
-> PASS
-> Human Summary
```

人間が各Taskを開くのは例外時のみ。

## 8. 実装優先度

最初に高度なSecurity Reviewerを作るより、以下を先に実装する。

```text
1. Validation TaskをQueueに載せられる
2. Coding Worker自己承認を禁止
3. Reviewerを別Executionで起動
4. Judge Taskをrequired reviews待ちにできる
5. Cost GovernorがReviewerにも効く
6. Evidenceと判定をAuditできる
```

この土台ができてからReviewerの知能を高度化する。
