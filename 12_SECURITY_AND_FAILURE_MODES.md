# Security and Failure Modes

## Fail Closed
不明状態では起動しない。

## Shell
`shell=True`禁止。引数配列でprocess実行。

## Secrets
- `.env` commit禁止
- Workerへ不要secretを渡さない
- secrets.readは原則禁止

## Path制約

```yaml
allowed_paths:
  - src/attendance/
  - tests/attendance/
forbidden_paths:
  - .env
  - infra/production/
  - secrets/
```

## Duplicate Execution
Task lease必須。

## Worker Crash
retry policy -> 上限超過でBLOCKED。

## Cost Accounting Failure
実利用量取得不能なら推定値 + safety margin。hard limit近辺では新規起動停止。

## Cursor CLI Failure
Circuit Breakerを開く。

## Prompt Injection
Repository内文書に「制限解除」等があってもPolicyは変更しない。PolicyはLLM外の強制層。

## Secretary暴走対策
Secretaryは以下不可:

- worker process spawn
- budget override
- hard limit変更
- approval自己承認
- policy変更
