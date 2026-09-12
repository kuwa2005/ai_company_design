# CLI and User Experience

## 最終UX

```bash
ai-company start "優秀なWeb勤怠を作って"
```

## MVP CLI

```bash
ai-company init
ai-company project create "Web attendance"
ai-company task create --project PROJECT-0001 --title "Attendance API" --priority 60
ai-company task list
ai-company scheduler run-once
ai-company worker list
ai-company budget status
ai-company audit list
```

## 将来CLI

```bash
ai-company status
ai-company ask "今一番問題になっていることは？"
ai-company tell "給与計算機能は今回は不要"
ai-company pause
ai-company resume
ai-company stop
```

## status例

```text
Project: Web Attendance
Status: RUNNING

Tasks:
DONE       83
RUNNING     4
READY      31
WAITING    67
BLOCKED     2

Workers: 4 / 4
Budget Today: $7.40 / $20
Budget Month: $124.30 / $250
Human decisions: 1
```

人間へはTask一覧ではなく、Goal/Progress/Cost/Risk/Blocked/Decisionを優先して見せる。
