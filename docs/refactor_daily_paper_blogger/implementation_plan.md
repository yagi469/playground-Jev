# daily_paper_blogger.py のモジュール分割リファクタリング計画

3,034 行の一枚岩（モノリス）スクリプトとなっている [daily_paper_blogger.py](file:///c:/Users/user/Dev/playground-Jev/daily_paper_blogger.py) を、既存の外部連携（CLI 引数、GitHub Actions、`backend/app.py` からの関数インポート）との **100% 後方互換性** を完全に維持したまま、保守性と拡張性に優れたクリーンなモジュール構造へとリファクタリングします。

## ユーザー確認事項 (User Review Required)

> [!IMPORTANT]
> **外部インターフェースの完全保証**
> - GitHub Actions（`.github/workflows/daily_blogger.yml`）で実行される以下のコマンド群は、引数の変更なしにそのまま動作します：
>   - `python daily_paper_blogger.py --queue`
>   - `python daily_paper_blogger.py --arxiv-id "$ARXIV_ID"`
>   - `python daily_paper_blogger.py -f "$FILE" -p "$PAGES" -c "$CHAPTER"`
> - `backend/app.py` からのインポート（`from daily_paper_blogger import run_daily_pipeline`）も `daily_paper_blogger.py` からの re-export により完全に維持されます。

## ディレクトリ構成とモジュール設計

```text
playground-Jev/
├── daily_paper_blogger.py          # [MODIFY] メインエントリーポイント / CLI / re-export (約180行)
├── config.py                       # [NEW] 環境変数、API初期化(Gemini/TypeSafe)、定数パス
├── core/                           # [NEW] コア共通機能
│   ├── __init__.py
│   ├── figure_extractor.py         # PDFからの図表抽出・Markdown画像埋め込み
│   ├── arxiv_client.py             # arXiv API取得・XMLパース・論文PDF取得
│   ├── doc_reader.py               # PDF/MD読解・目次解析・ノンブルオフセット・自動スリム化
│   ├── book_queue.py               # book_queue.json の管理とタスク特定
│   └── post_formatter.py           # yagibrary (Astro) Frontmatter整形・過去記事検索
├── generators/                     # [NEW] 記事生成・推敲エンジン
│   ├── __init__.py
│   ├── prompts.py                  # ジャンル別プロンプト設定 (get_genre_blog_config)
│   ├── paper_generator.py          # arXiv論文用 Gemini執筆・Jev推敲ループ
│   └── doc_generator.py            # 書籍・ドキュメント用 Gemini執筆・Jev推敲ループ
└── pipelines/                      # [NEW] パイプライン実行層
    ├── __init__.py
    ├── daily_pipeline.py           # run_daily_pipeline (arXiv日次自動実行)
    ├── targeted_pipeline.py        # run_targeted_pipeline (指定arXiv ID)
    ├── file_pipeline.py            # run_file_pipeline (ローカルファイル単発)
    └── queue_pipeline.py           # run_queue_pipeline (書籍キュー自律連載)
```

## 提案する変更内容 (Proposed Changes)

### 1. 共通基盤層 (Configuration & Core)

#### [NEW] [config.py](file:///c:/Users/user/Dev/playground-Jev/config.py)
- `.env.local` / `.env` のロード
- `DEFAULT_YAGIBRARY_POSTS_DIR`, `DEFAULT_BOOK_QUEUE_PATH` などの定数
- `init_gemini_client()` シングルトン
- `typesafe_client` の初期化

#### [NEW] [core/figure_extractor.py](file:///c:/Users/user/Dev/playground-Jev/core/figure_extractor.py)
- `extract_pdf_figures`
- `fetch_arxiv_paper_figures`
- `build_figures_prompt_components`
- `embed_figures_in_markdown`

#### [NEW] [core/arxiv_client.py](file:///c:/Users/user/Dev/playground-Jev/core/arxiv_client.py)
- `_parse_arxiv_xml`
- `fetch_arxiv_papers`
- `fetch_arxiv_papers_by_ids`
- `fetch_arxiv_paper_content`

#### [NEW] [core/doc_reader.py](file:///c:/Users/user/Dev/playground-Jev/core/doc_reader.py)
- `resolve_document_path`
- `extract_pdf_pages_bytes`
- `detect_pdf_nombre_offset`
- `apply_offset_to_pages_str`
- `resolve_chapter_pages_from_toc`
- `load_and_process_local_file`（自動スリム化ロジック含む）

#### [NEW] [core/book_queue.py](file:///c:/Users/user/Dev/playground-Jev/core/book_queue.py)
- `load_book_queue`
- `save_book_queue`
- `get_next_queue_task`

#### [NEW] [core/post_formatter.py](file:///c:/Users/user/Dev/playground-Jev/core/post_formatter.py)
- `get_existing_arxiv_ids`
- `is_paper_already_blogged`
- `load_existing_posts_index`
- `find_relevant_past_posts`
- `format_post_for_yagibrary`
- `format_doc_post_for_yagibrary`

---

### 2. 生成・推敲層 (Generators)

#### [NEW] [generators/prompts.py](file:///c:/Users/user/Dev/playground-Jev/generators/prompts.py)
- `get_genre_blog_config`: `business`, `tech`, `physics`, `classics`, `general` の各ジャンルのペルソナ、構成、ガイドラインを管理

#### [NEW] [generators/paper_generator.py](file:///c:/Users/user/Dev/playground-Jev/generators/paper_generator.py)
- `load_user_interests`
- `screen_and_rank_papers_with_jev`
- `write_blog_post_with_gemini`
- `rewrite_blog_post_with_gemini`
- `generate_refined_blog_post`

#### [NEW] [generators/doc_generator.py](file:///c:/Users/user/Dev/playground-Jev/generators/doc_generator.py)
- `write_blog_post_from_doc_with_gemini`
- `rewrite_doc_blog_post_with_gemini`
- `generate_refined_doc_blog_post`

---

### 3. パイプライン層 & エントリーポイント (Pipelines & Main)

#### [NEW] [pipelines/daily_pipeline.py](file:///c:/Users/user/Dev/playground-Jev/pipelines/daily_pipeline.py)
- `run_daily_pipeline(max_papers, top_n_to_blog, output_dir)`

#### [NEW] [pipelines/targeted_pipeline.py](file:///c:/Users/user/Dev/playground-Jev/pipelines/targeted_pipeline.py)
- `run_targeted_pipeline(arxiv_ids, output_dir)`

#### [NEW] [pipelines/file_pipeline.py](file:///c:/Users/user/Dev/playground-Jev/pipelines/file_pipeline.py)
- `run_file_pipeline(...)`

#### [NEW] [pipelines/queue_pipeline.py](file:///c:/Users/user/Dev/playground-Jev/pipelines/queue_pipeline.py)
- `run_queue_pipeline(...)`

#### [MODIFY] [daily_paper_blogger.py](file:///c:/Users/user/Dev/playground-Jev/daily_paper_blogger.py)
- 主要関数の re-export（後方互換性担保）
- CLI 引数パース（`argparse`）と適切なパイプライン関数へのディスパッチのみを行う薄いオーケストレーター（約180行）に改修

---

## 検証計画 (Verification Plan)

### 自動テスト / コマンド検証
1. **構文・インポート検証**:
   - `python -c "import daily_paper_blogger; print('daily_paper_blogger import OK')"`
   - `python -c "from daily_paper_blogger import run_daily_pipeline, run_targeted_pipeline, run_file_pipeline, run_queue_pipeline; print('Re-exports OK')"`
   - `python -c "from backend.app import app; print('Backend app import OK')"`
2. **CLI 互換性検証**:
   - `python daily_paper_blogger.py --help`
   - `python daily_paper_blogger.py --dry-run-queue`
3. **ローカルドキュメント読解テスト**:
   - `python -c "from core.doc_reader import load_and_process_local_file; print('doc_reader OK')"`
