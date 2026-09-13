# Validation Orchestration

## 1. 目的

Coding Workerが出した成果を、そのCoding Worker自身の自己評価に依存せず検証する。

## 2. Reviewer種別

### Test Reviewer
- 受入条件から独立テスト観点を作る
- 既存テストが誤っていないか確認
- 回帰テスト不足を指摘

### Spec Reviewer
- 要求と実装の意味的一致
- 未定義仕様の勝手な補完
- acceptance criteria漏れ

### Security Reviewer
- authn/authz
- input validation
- secret exposure
- injection
- unsafe defaults
- permission boundary

### Edge-case Reviewer
- error handling
- timeout
- partial failure
- race / duplicate
- boundary values
- retry behavior

### Architecture Reviewer
高リスク変更のみ。
- layer violation
- coupling
- dependency direction
- data integrity
- migration risk

## 3. Validation Plan

Task riskから必要Reviewerを決める。

```yaml
risk_profiles:
  low:
    required:
      - test
  medium:
    required:
      - test
      - spec
  high:
    required:
      - test
      - spec
      - security
      - edge_case
  critical:
    required:
      - test
      - spec
      - security
      - edge_case
      - architecture
```

## 4. Reviewer output schema

```json
{
  "verdict": "PASS|FAIL|UNCERTAIN",
  "summary": "...",
  "findings": [
    {
      "severity": "critical|high|medium|low|info",
      "category": "...",
      "description": "...",
      "evidence": ["file:line", "test-name", "command-output"],
      "recommended_fix": "..."
    }
  ],
  "confidence": 0.92
}
```

## 5. Judge input

Judgeへ渡すもの:

- original goal / acceptance criteria
- Coding Worker result
- changed files / diff summary
- required reviewer list
- all Validation Results
- budget / policy status

## 6. Judge output

```json
{
  "verdict": "PASS|REJECT|HUMAN_ESCALATION",
  "blocking_findings": [],
  "non_blocking_findings": [],
  "conflicts": [],
  "repair_tasks": [],
  "reason": "..."
}
```

## 7. Judge Policy

LLM Judgeへ全権を渡さない。
コード側でも最低限のhard ruleを持つ。

例:

```text
critical finding exists -> cannot PASS
required reviewer missing -> cannot PASS
security FAIL -> cannot PASS
budget accounting invalid -> cannot PASS
```

その上でJudge AIが曖昧な複数結果を統合する。

## 8. Repair Loop

```text
REJECT
-> Repair Task
-> Coding Worker
-> NEW Validation Plan Execution
-> Reviewers
-> Judge
```

古いReview Resultを修正後のPASS根拠に使い回さない。

## 9. 無限ループ防止

```yaml
repair_loop:
  max_cycles: 3
  max_total_worker_runs: 12
```

超過:

```text
HUMAN_ESCALATION
```

## 10. Cost Control

Validationは品質向上のための無限fan-outをしない。

- risk-based reviewer count
- overall concurrency cap
- validation concurrency cap
- reviewer per-task cap
- monthly hard budget
- repair cycle cap

すべてCost Governor経由。
