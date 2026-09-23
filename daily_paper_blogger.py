"""
daily_paper_blogger.py
arXiv / Book Queue / Document × TypeSafe Jev × Gemini 自律型ブログ執筆パイプライン
エントリポイント ＆ 後方互換性維持モジュール
"""

import sys
import argparse
from typing import Optional, List, Dict, Any

# 1. 共通設定・クライアント
from config import (
    GEMINI_API_KEY,
    TYPESAFE_API_KEY,
    DEFAULT_YAGIBRARY_POSTS_DIR,
    DEFAULT_YAGIBRARY_STATIC_DIR,
    client,
    jev_client,
)

# 2. コアモジュール
from core.figure_extractor import extract_pdf_figures, extract_figures_from_pdf
from core.arxiv_client import fetch_arxiv_papers, fetch_arxiv_papers_by_ids
from core.doc_reader import load_and_process_local_file
from core.book_queue import (
    DEFAULT_BOOK_QUEUE_PATH,
    load_book_queue,
    save_book_queue,
    get_next_queue_task,
)
from core.post_formatter import (
    load_existing_posts_index,
    find_relevant_past_posts,
    format_post_for_yagibrary,
    format_doc_post_for_yagibrary,
)

# 3. 生成・推敲モジュール
from generators.prompts import get_genre_blog_config
from generators.paper_generator import (
    generate_refined_blog_post,
    screen_and_rank_papers_with_jev,
)
from generators.doc_generator import generate_refined_doc_blog_post
from score_post import verify_post_with_jev

screen_papers_with_jev = screen_and_rank_papers_with_jev
score_blog_post_with_jev = verify_post_with_jev

# 4. パイプライン実行モジュール
from pipelines.daily_pipeline import run_daily_pipeline
from pipelines.targeted_pipeline import run_targeted_pipeline
from pipelines.file_pipeline import run_file_pipeline
from pipelines.queue_pipeline import run_queue_pipeline

# 後方互換性のためのエクスポート
__all__ = [
    # 設定
    "GEMINI_API_KEY",
    "TYPESAFE_API_KEY",
    "DEFAULT_YAGIBRARY_POSTS_DIR",
    "DEFAULT_YAGIBRARY_STATIC_DIR",
    "DEFAULT_BOOK_QUEUE_PATH",
    "client",
    "jev_client",
    # コア
    "extract_pdf_figures",
    "extract_figures_from_pdf",
    "fetch_arxiv_papers",
    "fetch_arxiv_papers_by_ids",
    "screen_and_rank_papers_with_jev",
    "screen_papers_with_jev",
    "load_and_process_local_file",
    "load_book_queue",
    "save_book_queue",
    "get_next_queue_task",
    "load_existing_posts_index",
    "find_relevant_past_posts",
    "format_post_for_yagibrary",
    "format_doc_post_for_yagibrary",
    # ジェネレータ
    "get_genre_blog_config",
    "generate_refined_blog_post",
    "score_blog_post_with_jev",
    "verify_post_with_jev",
    "generate_refined_doc_blog_post",
    # パイプライン
    "run_daily_pipeline",
    "run_targeted_pipeline",
    "run_file_pipeline",
    "run_queue_pipeline",
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="arXiv / Book Queue / Document × TypeSafe Jev × Gemini 自律型ブログ執筆パイプライン"
    )
    parser.add_argument(
        "--queue", "-q",
        action="store_true",
        default=False,
        help="書籍キュー (book_queue.json) から次の未公開章を自律執筆（デフォルト動作）"
    )
    parser.add_argument(
        "--queue-path",
        type=str,
        default=DEFAULT_BOOK_QUEUE_PATH,
        help="書籍キューファイルのパス"
    )
    parser.add_argument(
        "--dry-run-queue",
        action="store_true",
        help="キューから次のタスクを特定・確認するが、記事執筆は行わない"
    )
    parser.add_argument(
        "--arxiv-daily",
        action="store_true",
        help="arXiv の日次自動スクリーニングモードを実行（旧デフォルト）"
    )
    parser.add_argument(
        "--arxiv-id", "-a",
        type=str,
        default="",
        help="特定の arXiv 論文番号（カンマ区切りで複数可。例: 2006.13892）"
    )
    parser.add_argument(
        "--file", "-f",
        type=str,
        default="",
        help="ローカルのPDFまたはMarkdownファイルパス（例: docs/high_output_management.pdf）"
    )
    parser.add_argument(
        "--pages", "-p",
        type=str,
        default="",
        help="PDFの対象ページ範囲（例: 15-30, 45）"
    )
    parser.add_argument(
        "--chapter", "-c",
        type=str,
        default="",
        help="フォーカスしたい章やテーマ（例: 'Chapter 1: The Basics of Production'）"
    )
    parser.add_argument(
        "--genre", "-g",
        type=str,
        default="auto",
        choices=["auto", "business", "tech", "physics", "classics", "history", "general"],
        help="執筆ジャンル (デフォルト: auto)"
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=None,
        help="書籍ノンブル（印刷ページ番号）とPDF通し番号の差分オフセット（手動指定）"
    )
    parser.add_argument(
        "--max-papers", "-m",
        type=int,
        default=50,
        help="arXivから自動取得する件数 (デフォルト: 50)"
    )
    parser.add_argument(
        "--top-n", "-n",
        type=int,
        default=3,
        help="ブログ記事化する上位件数 (デフォルト: 3)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default=None,
        help="記事保存先ディレクトリ"
    )
    parser.add_argument(
        "--filename", "--custom-filename",
        type=str,
        default=None,
        help="保存する記事ファイル名を明示的に指定（例: my-custom-article.md）"
    )
    parser.add_argument(
        "positional_args",
        nargs="*",
        help="後方互換用: [max_papers] [top_n]"
    )

    args = parser.parse_args()

    # 位置引数があれば上書き (python script.py 10 3 など)
    if args.positional_args:
        try:
            args.max_papers = int(args.positional_args[0])
            if len(args.positional_args) > 1:
                args.top_n = int(args.positional_args[1])
        except ValueError:
            pass

    if args.file.strip():
        # ローカルファイル（PDF/MD）指定モード
        run_file_pipeline(
            file_path=args.file.strip(),
            pages=args.pages.strip() or None,
            chapter=args.chapter.strip() or None,
            genre=args.genre,
            offset=args.offset,
            output_dir=args.output_dir,
            custom_filename=args.filename,
        )
    elif args.arxiv_id.strip():
        # 特定論文指定モード
        target_ids = [aid.strip() for aid in args.arxiv_id.split(",") if aid.strip()]
        run_targeted_pipeline(arxiv_ids=target_ids, output_dir=args.output_dir)
    elif args.arxiv_daily:
        # arXiv 自動スクリーニングモード
        run_daily_pipeline(
            max_papers=args.max_papers,
            top_n_to_blog=args.top_n,
            output_dir=args.output_dir,
        )
    else:
        # デフォルト: 書籍キュー自動連載モード
        run_queue_pipeline(
            queue_path=args.queue_path,
            dry_run=args.dry_run_queue,
            output_dir=args.output_dir,
        )
