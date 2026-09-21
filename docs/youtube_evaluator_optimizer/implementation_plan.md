# YouTube自動記事化 GitHub Actions ワークフロー実装計画

YouTube動画のURLやセクション・時間指定からブログ記事を自動生成・推敲し、`yagibrary` に自動公開する GitHub Actions ワークフローを構築します。

## 1. 概要と目標
- **手動実行 (workflow_dispatch)**: GitHub の Actions 画面から URL・フォーカス・時間帯を指定してワンクリック実行。
- **Issue Ops (Issue トリガー)**: GitHub Issue を作成し、タイトルまたは本文に YouTube URL（およびフォーカス指定）を記載するだけで完全自動記事化。
- **NotebookLM 認証の CI 連携**: GitHub Secrets (`NOTEBOOKLM_COOKIES`) を経由して Ubuntu ランナー上で `nlm` CLI をヘッドレス動作させる。
- **yagibrary への自動反映**: 記事生成・推敲完了後、`yagibrary` リポジトリに自動コミット＆プッシュし、CloudFront/Astroの自動ビルド＆デプロイを発火。
- **Issue への自動フィードバック**: Issue 起因の場合は、生成された記事のタイトル・要約・リンクを自動コメントして Issue を自動クローズ。

---

## 2. 変更・追加対象ファイル

### 1. [NEW] [youtube_blogger.yml](file:///c:/Users/user/Dev/playground-Jev/.github/workflows/youtube_blogger.yml)
- `workflow_dispatch` および `issues` イベントをハンドリングする GitHub Actions 定義ファイル。
- `uv` による `notebooklm-mcp-cli` のインストール、Cookie復元、`youtube_to_blog.py` の実行、Git コミット＆プッシュ、Issue への返信を定義。

### 2. [NEW] [export_cookies.py](file:///c:/Users/user/Dev/playground-Jev/export_cookies.py)
- ローカルですでに認証済みの `~/.notebooklm-mcp-cli/profiles/default/cookies.json` を読み出し、GitHub Secrets に貼り付け可能な形式で出力・クリップボードにコピーする便利な支援スクリプト。

### 3. [MODIFY] [youtube_to_blog.py](file:///c:/Users/user/Dev/playground-Jev/youtube_to_blog.py)
- Issue 本文のテキストから YouTube URL や `--focus`、`--time` などの引数を抽出・解釈するパーサー機能（`--from-text` または Issue 解析モード）を追加。

---

## 3. 必要な GitHub Secrets

以下の Secrets を `playground-Jev` リポジトリに設定します：

| Secret名 | 用途 | 既存設定状況 |
| :--- | :--- | :---: |
| `GH_PAT` | `yagibrary` へのアクセス・コミット権限 | 設定済み |
| `TYPESAFE_API_KEY` | TypeSafe Jev 多面品質評価 API | 設定済み |
| `GEMINI_API_KEY` | Gemini 自律推敲リライト API | 設定済み |
| `NOTEBOOKLM_COOKIES` | NotebookLM の認証 Cookie（JSON文字列） | **新規登録が必要** |

---

## 4. 検証手順

1. ワークフローファイルの構文検証（`actionlint` または YAML チェック）。
2. `export_cookies.py` によるローカル Cookie の正常読み出し確認。
3. `youtube_to_blog.py` のテキスト解析引数の単体動作テスト。
4. コミット＆プッシュし、GitHub Secrets の設定手順を案内。
