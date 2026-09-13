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


## 11. 誤ったテストへの対策

Coding Workerが作成したテストだけを唯一の真実にしない。
Test ReviewerはAcceptance Criteriaから独立して追加検証観点を生成できる。

## 12. 仕様の穴への対策

Spec Reviewerは、仕様に書いていない挙動を実装者が勝手に確定していないか確認する。
曖昧性が重大なら`UNCERTAIN`としてJudgeへ送り、必要ならHumanへEscalationする。

## 13. Security shortcutへの対策

Security Reviewerは、機能テスト成功とは独立して認証・認可・入力検証・秘密情報・危険な依存関係・権限境界を確認する。

## 14. Error / Edge-case欠落への対策

Edge-case Reviewerはhappy pathだけでなく、timeout、null/empty、重複、競合、リトライ、部分失敗、境界値、外部依存失敗を検証する。

## 15. AI同士の同質誤り

同一モデル・同一promptによる複数Reviewerは完全な独立性を保証しない。
将来は複数モデル / provider / prompt familyを組み合わせる。
ただしCursor内で複数モデルを利用する場合でも、Cost Governorと同じQueue統制下に置く。
