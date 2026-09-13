# ノールック開発は正しいか - 本システムへの設計反映

## 1. 結論

ノールック開発は「コードを見ないから正しい／間違い」という二択ではない。

人間が逐次コードを読まなくても成立するためには、**人間が担っていた警戒・レビュー・テスト・セキュリティ確認を、独立したAI検証系へ構造化して移管する必要がある。**

本システムではこれを「No-look Validation Gate」と呼ぶ。

## 2. AI Coding Agentの主な失敗モード

### 2.1 それらしい誤実装
コンパイルもテストも通るが、要求意図と違う実装をする。

対策:
- Spec Reviewer
- Acceptance Criteria照合
- 独立テスト

### 2.2 テストそのものを間違える
実装者が自分の解釈に合わせてテストを書き、誤実装と誤テストが同時に整合する。

対策:
- Test Reviewerは実装者のテストを読むだけでなく、仕様から独立テスト観点を生成
- Coding Workerのtest passを最終証拠にしない

### 2.3 仕様の穴を都合よく解釈する
曖昧な要件を勝手に埋めてしまう。

対策:
- Spec Reviewer
- ambiguity finding
- Judgeの`HUMAN_ESCALATION`

### 2.4 セキュリティ上危険な近道
動けばよいという局所最適で認証・認可・入力検証等を弱める。

対策:
- Security Reviewer
- high-risk TaskではSecurity Review必須

### 2.5 Error / Edge-caseの欠落
happy pathだけ実装し、timeout・競合・部分失敗・境界条件を落とす。

対策:
- Edge-case Reviewer
- fault / negative scenario checklist

## 3. 属人化しない

上記リスクを「熟練エンジニアが目視するから大丈夫」という形だけに戻さない。

以下を再利用可能なAgent Profile / Skill / Promptとして実装する。

```text
Test Reviewer
Spec Reviewer
Security Reviewer
Edge-case Reviewer
Architecture Reviewer
Judge
```

検証能力自体をソフトウェア資産として蓄積する。

## 4. 複数AI検証

単一Reviewerも誤る。
高リスクTaskでは複数の独立Reviewerを使用する。

```text
Coding Result
  +-> Test Reviewer
  +-> Spec Reviewer
  +-> Security Reviewer
  +-> Edge-case Reviewer
  +-> Architecture Reviewer (optional)
          |
          v
         Judge
```

理想的にはモデル・provider・prompt familyも分散する。
ただし多重起動はCost Governorの上限内で行う。

## 5. Judgeが必要な理由

複数Reviewerを起動しただけでは、人間が5つのレビューを読む必要があり、目的を達成しない。

Judgeが以下を行う。

- findings重複排除
- severity正規化
- conflicting verdict検出
- evidence確認
- PASS / REJECT / HUMAN_ESCALATION
- Repair Taskへの変換

人間が見るのは原則Judgeの要約だけ。

## 6. PASS条件

最低条件:

```text
required reviewers completed
AND no blocking finding
AND judge == PASS
AND policy satisfied
AND budget/accounting valid
```

Coding Workerの`success=true`だけではPASSしない。

## 7. No-lookの意味

悪いNo-look:

```text
AIが作った
-> AIがテスト通ったと言った
-> 完成
```

本システムのNo-look:

```text
AIが作った
-> 別AI群が異なる観点で検証
-> JudgeがEvidenceを統合
-> 異常時は自動差し戻し
-> 解決不能な例外だけ人間へ
```

## 8. 人間の役割

人間を完全排除するのではない。
人間の仕事をTaskレビューから例外・方針判断へ移す。

- business requirement decision
- unresolved ambiguity
- major security tradeoff
- budget override
- production release（初期段階）

## 9. 本設計への影響

この方針は以下を変更する。

- Queue: Validation Task / Judge Task追加
- Scheduler: Reviewer独立性、Validation concurrency追加
- Cost Governor: reviewer fan-out予算追加
- Runner: reviewer/judge profile追加
- Data Model: Validation Plan / Result / Judge Decision追加
- Audit: Evidenceと判定根拠追加
- Roadmap: Milestone 7を全面改訂、Judgeを独立Milestone化

詳細は`16_MILESTONE_IMPACT_ANALYSIS.md`を参照。
