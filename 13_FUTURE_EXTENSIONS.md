# Future Extensions

## 優先して実装したい将来マイルストーン

### Milestone 11 - Local Multi-Project Web UI

ローカルで複数案件を取り扱う操作面。毎回 CLI を叩かなくてよいことが目的。

- 実行環境: Agent が動くローカルマシン（レンタルサーバー前提ではない）
- 案件ポートフォリオ（複数 project / workspace）
- Goal 投入・autonomous 開始/停止
- 進捗 / Budget / Staffing / Audit
- Exception-only（人間判断キュー）
- localhost Web UI + 薄い API（CLI と同等操作）
- Queue → Scheduler → Cost Governor 不変条件は維持

詳細は `10_IMPLEMENTATION_ROADMAP.md` の Milestone 11 を参照。

---

## その他の拡張候補

- Git worktree per Worker
- Docker sandbox
- multi-repository
- GitHub Issues / PR
- CI/CD
- Slack / email notifications
- 1日2回の要約
- Corporate Memory
- Decision Log
- PRD Agent
- FDE Agent
- Product Agent
- Architect Agent
- Reviewer Agent
- Security Agent
- UX Evaluator
- Performance Evaluator
- model routing
- cost optimization
- worker profile scoring
- prompt profile versioning
- project portfolio management
- cron / event-driven jobs
- Web UI（→ M11 で具体化）
- API（→ M11 と一体）
- MCP

- Multi-model cross validation
- Cursor内モデル切替によるReviewer diversity
- 外部Codex / Claude等の独立検証Adapter
- Reviewer performance scoring
- Judge calibration
- Validation coverage metrics
- Human override learning / policy refinement
