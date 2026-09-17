# Cursor CLI（Agent）日本語ガイド

この文書は、`agent --help` に表示される内容を、日本語で用途別に整理した解説です。

> 対象コマンド：`agent`
>
> 基本形式：`agent [options] [command] [prompt...]`

## 1. Cursor Agentで何ができるか

| 分類 | できること | 主な指定・コマンド |
| --- | --- | --- |
| 対話型AIエージェント | ターミナル上でCursor Agentと対話し、調査・説明・コード作成などを依頼する | `agent`、`agent "依頼内容"` |
| 非対話実行 | AIの回答を標準出力へ出し、シェルスクリプトや自動処理から利用する | `--print` |
| 構造化出力 | 結果を通常テキスト、JSON、ストリーミングJSONで受け取る | `--output-format` |
| 読み取り専用の検討 | ファイルを変更せず、調査や実装計画だけを作らせる | `--plan`、`--mode plan` |
| 質問・解説 | ファイルを変更せず、コードや技術について質問する | `--mode ask` |
| AIモデル選択 | GPT、Claudeなど、アカウントで利用可能なモデルを指定する | `--model`、`models` |
| セッション継続 | 過去または直前の会話を再開する | `--resume`、`--continue`、`resume`、`ls` |
| ワークスペース指定 | Agentが作業するフォルダや、追加で参照できるフォルダを指定する | `--workspace`、`--add-dir` |
| Git作業の分離 | 現在の作業ツリーを汚さず、隔離されたGit worktreeで作業する | `--worktree` |
| 実行承認の制御 | コマンドやツール実行を自動承認または安全性に応じて承認する | `--force`、`--auto-review` |
| サンドボックス制御 | Agentの作業をサンドボックス内に制限するか明示する | `--sandbox` |
| MCP連携 | 外部ツールやサービスへ接続するMCPサーバーを管理する | `mcp`、`--approve-mcps` |
| プラグイン利用 | ローカルプラグインの読込みや、プラグイン／マーケットプレイスを管理する | `--plugin-dir`、`plugin` |
| 認証・状態確認 | Cursorへのログイン、ログアウト、ログイン状態の確認を行う | `login`、`logout`、`status`、`whoami` |
| 切断に強い長時間実行 | ターミナルが切断されても存続するセッションを開始・管理する | `persist` |
| 自前マシンでの実行 | Cursor Cloud Agentからの処理を受け取るセルフホスト型workerを動かす | `worker` |
| AWS Bedrock連携 | Cursor CLIで使うAWS Bedrockを設定する | `bedrock` |
| Cursorルール作成 | 対話形式で新しいCursor Ruleを生成する | `generate-rule`、`rule` |
| 更新・環境情報確認 | CLIの更新、バージョン、システム、アカウント情報を確認する | `update`、`about`、`--version` |

## 2. 最も基本的な使い方

### 対話モードで起動する

```bash
agent
```

Cursor Agentが起動し、ターミナル上で対話しながら作業できます。

### 最初の依頼を付けて起動する

```bash
agent "このプロジェクトの構成を説明して"
```

`prompt` はAgentに最初に渡す依頼文です。複数の単語を含む場合は、引用符で囲むと安全です。

### 1回だけ実行して結果を表示する

```bash
agent --print "README.mdを要約して"
```

`--print` は、対話画面を使わず結果を標準出力へ出します。バッチ処理やスクリプトからの利用に向いています。

> **重要:** `--print` でも、Agentはファイル書込みやシェル実行を含む全ツールへアクセスできます。「画面に答えを表示するだけ」という意味ではありません。変更させたくない場合は `--mode ask` または `--plan` も指定します。

## 3. オプション一覧

### 基本・接続・認証

| オプション | 意味 | 使用例・補足 |
| --- | --- | --- |
| `-v`, `--version` | Cursor Agentのバージョンを表示して終了する | `agent --version` |
| `--api-key <key>` | 認証に使うAPIキーを直接指定する | 環境変数 `CURSOR_API_KEY` でも指定可能。コマンド履歴にキーが残る可能性があるため、通常は環境変数が扱いやすい |
| `-H`, `--header <header>` | AgentのAPIリクエストへ独自HTTPヘッダーを追加する | `-H 'Name: Value'`。複数回指定可能 |
| `-e`, `--endpoint <url>` | 接続先APIエンドポイントを変更する | 環境変数 `CURSOR_API_ENDPOINT` でも指定可能。既定値は `https://api2.cursor.sh` |
| `-h`, `--help` | ヘルプを表示する | `agent --help` |

### 出力・自動処理

| オプション | 意味 | 使用例・補足 |
| --- | --- | --- |
| `-p`, `--print` | Agentの回答を標準出力に表示する非対話モード | スクリプト、CI、バッチ処理向け。書込み・シェルを含む全ツールを利用可能 |
| `--output-format <format>` | `--print` 時の出力形式を選ぶ | `text`、`json`、`stream-json`。既定値は `text` |
| `--stream-partial-output` | 生成途中の文章を差分単位で順次出力する | `--print --output-format stream-json` と組み合わせる |

### 実行モード

| オプション | 意味 | ファイル変更 |
| --- | --- | --- |
| `--mode plan` | プロジェクトを分析し、実装方針や計画を提案する | しない（読み取り専用） |
| `--plan` | `--mode=plan` の短縮形 | しない（読み取り専用） |
| `--mode ask` | 質問への回答やコードの説明を行うQ&Aモード | しない（読み取り専用） |
| モード指定なし | 通常のAgentとして作業する | 依頼と承認設定により変更・コマンド実行が可能 |

### セッションの再開

| オプション | 意味 | 使用例・補足 |
| --- | --- | --- |
| `--resume [chatId]` | 過去のセッションを選択して再開する | Chat IDを省略した場合は、再開対象を選ぶ動作になる |
| `--continue` | 直前のセッションを続ける | 前回の文脈を引き継いで作業したい場合に使用 |

### AIモデル

| オプション | 意味 | 使用例・補足 |
| --- | --- | --- |
| `--model <model>` | 使用するAIモデルを指定する | 例：`--model gpt-5`、`--model sonnet-4-thinking` |
| `--list-models` | 利用可能なモデルを一覧表示して終了する | `agent --list-models` |

一部のモデルは、角括弧内で追加設定を上書きできます。

```bash
agent --model 'claude-opus-4-8[context=1m,effort=high,fast=false]'
```

角括弧をシェルに解釈されないよう、モデル指定全体をシングルクォートで囲みます。利用できるモデルやパラメータは、アカウントや時期によって異なる可能性があるため、`agent models` または `agent --list-models` で確認します。

### 承認と安全性

| オプション | 意味 | 注意点 |
| --- | --- | --- |
| `-f`, `--force` | 明示的に拒否されていないコマンドを強制的に許可する | 危険な操作や意図しない変更も自動実行され得るため、信頼できるリポジトリと明確な指示でのみ使用する |
| `--yolo` | `--force` の別名 | 動作は `--force` と同じ |
| `--auto-review` | サーバー側の分類器が安全と判定したツール呼出しを自動実行し、それ以外は確認を求める | 全自動許可より慎重で、承認回数を減らしたい場合に向く |
| `--sandbox enabled` | サンドボックスを明示的に有効にする | 設定ファイルの値より優先される |
| `--sandbox disabled` | サンドボックスを明示的に無効にする | Agentが広い範囲へ影響を与えられる可能性があるため注意 |
| `--approve-mcps` | すべてのMCPサーバーを自動承認する | 接続先MCPが信頼できることを確認して使う |
| `--trust` | 現在のワークスペースを、確認なしで信頼する | 内容が不明なリポジトリでは安易に指定しない |

#### 承認方式の大まかな使い分け

| 目的 | 推奨候補 |
| --- | --- |
| 説明だけ聞きたい | `--mode ask` |
| 変更前に計画だけ確認したい | `--plan` |
| 安全な処理は自動化し、危険な処理だけ確認したい | `--auto-review` |
| 隔離環境で自動実行したい | `--worktree` と `--auto-review`、必要に応じて `--sandbox enabled` |
| すべてを無確認で走らせたい | `--force`。影響範囲を十分に限定できる場合のみ |

### ワークスペース・追加フォルダ

| オプション | 意味 | 使用例・補足 |
| --- | --- | --- |
| `--workspace <path-or-name>` | 作業対象のディレクトリ、または保存済みワークスペース名を指定する | 省略時は現在のディレクトリ |
| `--add-dir <path>` | 追加のワークスペースルートを指定する | 複数回指定可能。別フォルダの仕様書などを参照させる場合に使える |
| `--plugin-dir <path>` | ローカルプラグインのディレクトリを読み込む | 複数回指定可能 |

例：別ディレクトリの資料も参照させる場合

```bash
agent --workspace ~/workspace/my-app \
  --add-dir ~/documents/specifications \
  "仕様書と実装の差分を調査して"
```

### Git worktree

| オプション | 意味 | 使用例・補足 |
| --- | --- | --- |
| `-w`, `--worktree [name]` | `~/.cursor/worktrees/<リポジトリ名>/<名前>` に隔離されたGit worktreeを作り、そこでAgentを起動する | 名前を省略すると自動生成される |
| `--worktree-base <branch>` | 新しいworktreeの起点となるブランチまたはrefを指定する | 省略時は現在の `HEAD` |
| `--skip-worktree-setup` | `.cursor/worktrees.json` に定義されたworktreeセットアップスクリプトを実行しない | 特別な理由で初期設定を省略したい場合に使用 |

例：`main` を起点に、`feature-login` という隔離環境で作業する場合

```bash
agent --worktree feature-login \
  --worktree-base main \
  "ログイン機能を実装してテストして"
```

## 4. サブコマンド一覧

| コマンド | できること | 補足 |
| --- | --- | --- |
| `persist` | ターミナル切断後も存続するセッションを開始・管理する | 長時間処理やリモート接続での作業向け |
| `install-shell-integration` | シェル統合を `~/.zshrc` にインストールする | ヘルプ上ではZsh向け |
| `uninstall-shell-integration` | `~/.zshrc` からシェル統合を削除する | シェル統合が不要になった場合に使用 |
| `login` | Cursorへログインする | `NO_OPEN_BROWSER` を設定するとブラウザの自動起動を無効化できる |
| `logout` | サインアウトし、保存された認証情報を消去する | 共用PCなどで作業を終える場合にも有用 |
| `mcp` | MCPサーバーを管理する | 詳細は `agent mcp --help` で確認 |
| `plugin` | プラグインとプラグインマーケットプレイスを管理する | 詳細は `agent plugin --help` で確認 |
| `worker` | セルフホスト型Cloud Agent workerをこのマシンで実行する | `--pool` なしは個人用My Machines。`--pool` ありはEnterprise向けチームプール |
| `status`, `whoami` | 現在の認証状態を確認する | どのアカウントでログイン中か確認できる |
| `models` | このアカウントで利用可能なモデルを一覧表示する | `--list-models` と同種の確認用途 |
| `bedrock` | AWS Bedrockの利用設定を行う | AWS Bedrock経由のモデル利用を構成するためのコマンド |
| `about` | バージョン、システム、アカウント情報を表示する | トラブル調査時の環境確認にも使える |
| `update` | Cursor Agentを最新版へ更新する | 更新後は `agent --version` で確認できる |
| `create-chat` | 空の新規チャットを作成し、Chat IDを返す | 自動処理で会話の入れ物だけ先に作る場合などに利用可能 |
| `generate-rule`, `rule` | 対話形式で新しいCursor Ruleを生成する | プロジェクト固有の開発規約やAIへの指示を作る用途 |
| `agent [prompt...]` | Cursor Agentを開始する | 実行ファイル名と同じ名称のサブコマンド |
| `ls` | チャットセッションを選んで再開する | 過去の会話を探して続ける用途 |
| `resume` | 最新のチャットセッションを再開する | 直前の作業を素早く続ける用途 |
| `help [command]` | 指定したサブコマンドの詳しいヘルプを表示する | 例：`agent help worker` |

## 5. 用途別の実行例

### コードを変更せず、構成だけ説明させる

```bash
agent --mode ask "このリポジトリの構成と主要モジュールを説明して"
```

### 実装前に計画を作らせる

```bash
agent --plan "認証機能を追加するための実装計画を作って"
```

### JSON形式で調査結果を取得する

```bash
agent --print --output-format json \
  --mode ask \
  "このプロジェクトで使われている主要技術を調査して"
```

### 生成途中の出力をストリーミングで受け取る

```bash
agent --print \
  --output-format stream-json \
  --stream-partial-output \
  "テスト結果を分析して"
```

### 使用モデルを指定する

```bash
agent --model gpt-5 "不具合の原因を調査して"
```

### 直前の会話を続ける

```bash
agent --continue
```

または、サブコマンドを使います。

```bash
agent resume
```

### 特定のチャットを再開する

```bash
agent --resume <chatId>
```

### 安全と判定された操作だけ自動承認する

```bash
agent --auto-review "テストを実行し、失敗原因を修正して"
```

### 隔離worktreeで作業させる

```bash
agent --worktree bugfix-123 \
  --auto-review \
  "Issue 123の不具合を修正してテストして"
```

### 利用可能なモデルを確認する

```bash
agent models
```

または次の指定でも確認できます。

```bash
agent --list-models
```

### ログイン状態を確認する

```bash
agent status
```

または次のように実行します。

```bash
agent whoami
```

### サブコマンド固有のオプションを確認する

```bash
agent help worker
agent mcp --help
agent plugin --help
```

## 6. 環境変数

| 環境変数 | 用途 | 対応オプション・コマンド |
| --- | --- | --- |
| `CURSOR_API_KEY` | Cursor APIの認証キー | `--api-key` |
| `CURSOR_API_ENDPOINT` | 接続先APIエンドポイント | `--endpoint` |
| `NO_OPEN_BROWSER` | `login` 時にブラウザを自動起動しない | `login` |

例：APIキーを環境変数で一時的に設定する場合

```bash
export CURSOR_API_KEY='ここにAPIキー'
agent --print --mode ask "このコードを説明して"
```

APIキーをコマンド引数に直接書くと、シェルの履歴やプロセス一覧に残る可能性があります。認証情報の保管方法は、利用環境のセキュリティ方針に合わせてください。

## 7. 迷ったときの選び方

| やりたいこと | コマンド例 |
| --- | --- |
| 普通に対話したい | `agent` |
| 最初から依頼を渡したい | `agent "依頼内容"` |
| 説明だけ聞き、変更させたくない | `agent --mode ask "質問"` |
| 計画だけ作らせたい | `agent --plan "依頼内容"` |
| スクリプトから1回実行したい | `agent --print "依頼内容"` |
| 機械処理しやすいJSONで受け取りたい | `agent --print --output-format json "依頼内容"` |
| 安全な操作を自動承認したい | `agent --auto-review "依頼内容"` |
| Gitの現在の作業を汚したくない | `agent --worktree task-name "依頼内容"` |
| 前回の続きから始めたい | `agent --continue` または `agent resume` |
| モデルを選びたい | `agent models` で確認後、`agent --model <model> "依頼内容"` |

## 8. 使用時の注意点

1. **`--print` は読み取り専用ではありません。** ファイル書込みやシェル実行も可能です。説明だけなら `--mode ask`、計画だけなら `--plan` を併用します。
2. **`--force` と `--yolo` は強力です。** 削除、上書き、外部送信などを確認なしで実行する可能性があります。対象フォルダ、許可する操作、禁止事項を依頼文で具体的に指定してください。
3. **不慣れなリポジトリでは `--trust` を安易に使わないでください。** 中にある設定や指示を確認してから信頼します。
4. **自動実装ではGit worktreeが有効です。** `--worktree` を使うと作業を分離しやすく、変更内容の比較や破棄が容易になります。
5. **追加ディレクトリは必要最小限にします。** `--add-dir` で渡した場所もAgentの参照・作業範囲になります。
6. **MCPの一括承認は接続先を確認してから行います。** `--approve-mcps` は、すべてのMCPサーバーを自動承認します。
7. **利用可能なモデル名は固定とは限りません。** 実行時点の一覧を `agent models` で確認するのが確実です。

## 9. ヘルプの掘り下げ方

今回の `agent --help` は全体の入口です。各機能の詳細は、次の形式で確認できます。

```bash
agent help <command>
```

または、サブコマンドに `--help` を付けます。

```bash
agent <command> --help
```

例：

```bash
agent persist --help
agent worker --help
agent mcp --help
agent plugin --help
agent bedrock --help
```

これらの出力を追加すれば、セッション常駐、MCP、プラグイン、worker、Bedrockなどについて、さらに詳しい日本語マニュアルへ拡張できます。

---

## まとめ

Cursor CLIの `agent` は、単なる質問用CLIではなく、次の機能をまとめて扱えるAI作業エージェントです。

- 対話による調査・設計・実装・テスト
- 読み取り専用の質問モードと計画モード
- スクリプトから使える非対話実行とJSON出力
- モデルの選択とセッションの継続
- ワークスペースやGit worktreeによる作業範囲の管理
- MCP、プラグイン、AWS Bedrockとの連携
- 自動承認、サンドボックス、信頼設定による実行制御
- 切断後も存続するセッションやセルフホストworkerの運用

最初は `agent --mode ask`、`agent --plan`、`agent --auto-review`、`agent --worktree` の4つを使い分けると、安全性と自動化のバランスを取りやすくなります。
