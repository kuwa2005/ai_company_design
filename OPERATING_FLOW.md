# 運用フロー（1枚）

人が打つコマンドと、機械が回す範囲の境界。

## 日常の流れ

```text
ai-company doctor          # 事前診断（副作用なし）
ai-company start "Goal" [--delivery-mode strict|prototype] [--runner cursor|--runner mock]
ai-company scheduler continue [--project ID]
ai-company status [--project ID] [--debug]
ai-company approval … / fire-supervisor …   # 例外時のみ
```

| コマンド | 人の役割 | 機械の役割 |
|---|---|---|
| `doctor` | 環境を確認 | PASS/WARN/FAIL と直し方を表示 |
| `start` | Goal を与える | Task Graph・人員依頼を作る（ワーカーは起動しない） |
| `scheduler continue` | 進める合図 | Lease → Cost → Worker（mock/cursor）→ Validation → Judge → 自問 |
| `status` | 読む | 実装消化／自問中／リリース可を分離表示 |
| `approval` | 例外を承認/却下 | 停止タスクを解除 |
| `fire-supervisor` | わからず屋を解雇 | 出荷優先で DONE＋残件引き継ぎ |

## 完了の意味

- **実装キュー空** ≠ Goal 完了
- **自問ループ CLEAR まで** = リリース候補
- `ship_first` / `prototype` で無視した FAIL は **残件タスク** に必ず残る
- デフォルトは `strict`（安全側）。デモは `--delivery-mode prototype`

## 検証の分担

| 役割 | 意味 |
|---|---|
| `test_reviewer` 等 | 実装ゲート（この差分は出荷してよいか） |
| `test_auditor` | メタ監査（緑でもテストが正しいか） |
| Judge hard rules | コード側の最終出荷判定 |

## デフォルト方針

- `delivery_mode: strict`
- `auto_fire_stubborn_supervisor: false`
- Reviewer / Judge は write 禁止（git diff で強制）
- Worker 自己申告の `changed_files` は Evidence Collector が照合
