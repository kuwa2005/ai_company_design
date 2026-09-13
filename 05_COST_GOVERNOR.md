# Cost Governor

## 1. 目的

Cursor多重起動による月額枠消費・追加課金暴走を防ぐ。
AIよりCost Governorが強い。

ノールック開発ではReviewerを複数起動するため、**Validation fan-outによるコスト増大も主要リスク**として扱う。

## 2. 判定

```text
ALLOW
THROTTLE
DEFER
REQUIRE_APPROVAL
DENY
```

## 3. 制限階層

- Concurrent workers
- Concurrent validation workers
- Hourly usage
- Daily usage
- Monthly usage
- Project budget
- Task budget
- Validation budget
- Retry budget
- Runtime budget

## 4. 例

```yaml
budget:
  hourly:
    max_runs: 20
    max_cost_usd: 5
  daily:
    max_runs: 100
    max_cost_usd: 20
  monthly:
    soft_limit_usd: 200
    hard_limit_usd: 250

validation:
  max_reviewers_per_task: 5
  max_parallel_reviewers_per_task: 2
  default_budget_ratio: 0.30

  risk_profiles:
    low:
      reviewers: 1
    medium:
      reviewers: 2
    high:
      reviewers: 4
    critical:
      reviewers: 5
      require_human_if_disagreement: true

task:
  max_runtime_minutes: 30
  max_retries: 2
  max_worker_runs: 3
```

## 5. 段階Throttle

```yaml
usage_policy:
  normal:   {threshold: 0.00, max_workers: 8}
  warning:  {threshold: 0.60, max_workers: 4}
  high:     {threshold: 0.80, max_workers: 2}
  critical: {threshold: 0.95, max_workers: 1, priority_minimum: 80}
  exhausted:{threshold: 1.00, max_workers: 0}
```

Validationも同じ総枠を消費する。
「品質確認だから別腹」という扱いは禁止。

## 6. Hard Stop

hard limit到達時は新規Worker起動禁止。
人間承認なしに解除しない。

ただし、すでに実装済みで検証未完了のTaskはDONEにせず`DEFERRED_BUDGET`または`HUMAN_ESCALATION`とする。

## 7. Budget Reservation

Task起動前に推定コストを予約する。

```text
usable = limit - spent - reserved
```

同時Workerが同じ残予算を重複使用する事故を防ぐ。

Validation Plan作成時も、必要Reviewer群 + Judge分を可能な範囲で予約する。

## 8. Adaptive Validation

コストを抑えるため、すべてのTaskで5 Reviewerを起動しない。

例:

```text
低リスクUI文言修正
-> Test Reviewer 1 + Judge

認証API変更
-> Test + Spec + Security + Edge-case + Judge
```

リスクベースで品質とコストを両立する。

## 9. Costが取得できない場合

実コストが取れない場合は:

- runtime
- run count
- model
- role
- historical average

から推定し `estimated=true` を保存。

## 10. Circuit Breaker

```yaml
circuit_breaker:
  failure_threshold: 5
  window_minutes: 10
  cooldown_minutes: 30
```

短時間に失敗が集中したら新規起動停止。
Reviewerが同じ原因で大量REJECTを返す場合も、無限修正ループに入れず上位へエスカレーションする。
