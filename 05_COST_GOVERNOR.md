# Cost Governor

## 目的

Cursor多重起動による月額枠消費・追加課金暴走を防ぐ。
AIよりCost Governorが強い。

## 判定

```text
ALLOW
THROTTLE
DEFER
REQUIRE_APPROVAL
DENY
```

## 制限階層

- Concurrent workers
- Hourly usage
- Daily usage
- Monthly usage
- Project budget
- Task budget
- Retry budget
- Runtime budget

## 例

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

task:
  max_runtime_minutes: 30
  max_retries: 2
  max_worker_runs: 3
```

## 段階Throttle

```yaml
usage_policy:
  normal:   {threshold: 0.00, max_workers: 8}
  warning:  {threshold: 0.60, max_workers: 4}
  high:     {threshold: 0.80, max_workers: 2}
  critical: {threshold: 0.95, max_workers: 1, priority_minimum: 80}
  exhausted:{threshold: 1.00, max_workers: 0}
```

## Hard Stop

hard limit到達時は新規Worker起動禁止。
人間承認なしに解除しない。

## Budget Reservation

Task起動前に推定コストを予約する。

```text
usable = limit - spent - reserved
```

同時Workerが同じ残予算を重複使用する事故を防ぐ。

## Costが取得できない場合

実コストが取れない場合は:

- runtime
- run count
- model
- historical average

から推定し `estimated=true` を保存。

## Circuit Breaker

```yaml
circuit_breaker:
  failure_threshold: 5
  window_minutes: 10
  cooldown_minutes: 30
```

短時間に失敗が集中したら新規起動停止。
