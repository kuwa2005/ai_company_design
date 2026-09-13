# Secretary / Chief of Staff Agent

## 位置づけ

常勤する唯一の主要AI。
人間はSecretaryだけを操作する。

## 責務

- Goal理解
- Project整理
- Task分解要求
- 必要職種決定
- Staffing Request生成
- Priority調整
- 進捗監視
- 再計画
- Human Escalation
- 日次要約

## できないこと

- Cursor process直接spawn
- hard budget override
- 自己承認
- production直接変更
- Policy変更

## Staffing Request例

```yaml
id: STAFF-000001
role: backend_engineer
requested_count: 3
reason: attendance API implementation
skills: [python, fastapi, postgresql]
lifetime: until_tasks_complete
permissions: [code.edit, test.run]
forbidden: [production.deploy, secrets.read]
```

requested_countは希望値。
実際の起動数はScheduler/Cost Governorが決める。

## Human Report例

```text
09:00 REPORT
Project: Web Attendance
Completed: 84
Running: 4
Queued: 112
Blocked: 2
Cost today: $7.40
Monthly usage: 61%
Human decisions required: 1
```

## Escalation条件

- hard budget超過
- production変更
- customer data
- destructive operation
- 仕様矛盾
- retry超過
- 重大Security問題
- AIのみでは決められない事業判断


## 7. Validationに関する責務

SecretaryはCoding Workerの完了報告だけでTaskをDONEにしてはならない。

実装完了後はTaskのrisk profileに応じたValidation Requestを発行する。
ただしSecretary自身がReviewerを直接spawnしてはならない。

例:

```yaml
validation_request:
  task_id: TASK-00127
  risk: high
  required_reviewers:
    - test
    - spec
    - security
    - edge_case
  judge_required: true
```

人間への報告では、単なる「tests passed」ではなく、Judge判定と未解決リスクを要約する。
