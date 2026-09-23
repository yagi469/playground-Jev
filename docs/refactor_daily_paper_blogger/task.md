# タスクリスト: daily_paper_blogger.py のモジュール分割リファクタリング

## 目標
現在 3,034 行の一枚岩（モノリス）になっている `daily_paper_blogger.py` を、完全な後方互換性（CLI引数、関数エクスポート、GitHub Actions連携）を維持したまま、保守性の高い小さなモジュールへ分割する。

## タスク一覧
- [x] **1. 調査・設計フェーズ**
  - [x] `daily_paper_blogger.py` の依存関係と呼び出し元（`backend/app.py`, `.github/workflows/daily_blogger.yml`）の調査
  - [x] モジュール構成と責務の分割設計
  - [x] 実装計画書（`implementation_plan.md`）の作成とユーザー承認
- [x] **2. 共通基盤モジュールの作成**
  - [x] `config.py`: 環境変数、APIクライアント初期化（Gemini, TypeSafe）、定数パス
  - [x] `core/figure_extractor.py`: PDFからの図表抽出・画像埋め込み
  - [x] `core/arxiv_client.py`: arXiv API取得・XMLパース・論文PDF取得
  - [x] `core/doc_reader.py`: ローカルPDF/MD読解・目次解析・ノンブルオフセット・自動スリム化
  - [x] `core/book_queue.py`: `book_queue.json` の管理とタスク特定
  - [x] `core/post_formatter.py`: yagibrary (Astro) Frontmatter整形・過去記事検索
- [x] **3. 生成・推敲エンジンの分離**
  - [x] `generators/prompts.py`: 各ジャンルのペルソナ・見出し構成・プロンプト設定（`get_genre_blog_config`）
  - [x] `generators/paper_generator.py`: arXiv論文用の Gemini執筆・Jev推敲ループ
  - [x] `generators/doc_generator.py`: ローカル書籍・PDF用の Gemini執筆・Jev推敲ループ
- [x] **4. パイプライン層とエントリーポイントの再構築**
  - [x] `pipelines/daily_pipeline.py`: `run_daily_pipeline`（arXiv日次自動実行）
  - [x] `pipelines/targeted_pipeline.py`: `run_targeted_pipeline`（指定arXiv ID）
  - [x] `pipelines/file_pipeline.py`: `run_file_pipeline`（ローカルPDF/Markdown）
  - [x] `pipelines/queue_pipeline.py`: `run_queue_pipeline`（書籍キュー自律連載）
  - [x] `daily_paper_blogger.py`: 各パイプラインを統合する薄いエントリーポイント（CLI / re-export 約220行）に改修
- [x] **5. 検証と動作確認**
  - [x] `backend/app.py` からのインポート互換性テスト
  - [x] `--queue`（書籍キュー）モードの dry-run テスト
  - [x] CLI オプションの後方互換性テスト（`--help`）
  - [x] Walkthrough ドキュメントの作成
