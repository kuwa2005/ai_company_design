# Product Vision

## 目的

人間が多数のCoding Agentを直接管理する必要をなくす。
人間が操作するのは原則1つの常勤Agentだけ。

```text
Company = 1 Persistent Secretary + N Ephemeral Cursor Workers
```

Nは常に変動し、仕事がなければ0。

## 人間会社との対応

| 人間会社 | 本システム |
|---|---|
| オーナー | Human Owner |
| 秘書 / Chief of Staff | Secretary Agent |
| 人事 | Staffing Engine |
| 経理 | Cost Governor |
| タスクボード | Task Queue |
| 配員担当 | Scheduler |
| 短期アルバイト | Ephemeral Cursor Worker |
| 上司レビュー | Reviewer Worker |
| 内部監査 | Audit Engine |
| 解雇 | terminate |

## Human Experience

人間はTaskを逐一開かない。
人間が見るもの:

- Goal
- Progress
- Cost
- Risk
- Exceptions
- Human Decision Required

## 最適化目標

```text
最小コスト
+ 安全な並列度
+ 高いThroughput
+ 品質維持
```

最大人数を目指さない。


## 7. ノールック開発の製品目標

人間はTask単位のコードレビューを常時行わない。
その代わり、実装Workerと独立した検証Worker群が品質を検証する。

```text
Coding Worker
   -> Validation Workers
      -> Judge
         -> PASS / REJECT
```

人間へ上げるのは、原則として以下のみ。

- AI間で判断が割れた
- 重大セキュリティ問題
- 仕様が矛盾している
- Cost / Policy overrideが必要
- 事業判断が必要

したがって本システムの目標は「人間レビューをゼロにすること」ではなく、**人間レビューを例外処理へ移動すること**である。
