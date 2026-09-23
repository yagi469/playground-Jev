"""pipelines/queue_pipeline.py
書籍キュー (book_queue.json) を読み込み、次の未公開章を自律執筆してキューを更新するパイプライン
"""

import os
from datetime import datetime
from typing import Optional, List, Dict, Any

from core.book_queue import (
    DEFAULT_BOOK_QUEUE_PATH,
    load_book_queue,
    save_book_queue,
    get_next_queue_task,
)
from pipelines.file_pipeline import run_file_pipeline


def run_queue_pipeline(
    queue_path: str = DEFAULT_BOOK_QUEUE_PATH,
    dry_run: bool = False,
    output_dir: Optional[str] = None,
) -> List[str]:
    """書籍キューを読み込み、次の未公開章を自律執筆してキューを更新する"""
    print("\n" + "=" * 65)
    print(" 📚 書籍連載自動キューパイプライン (Book Queue Runner) 起動")
    print(f" 📂 キューファイル: {queue_path}")
    print("=" * 65)

    queue_data = load_book_queue(queue_path)
    task = get_next_queue_task(queue_data)
    if not task:
        print(" 🎉 すべての書籍の全章がすでに公開済みです。キューに未処理タスクはありません。")
        return []

    book_id = task["book_id"]
    book = task["book"]
    chapter = task["chapter"]
    ch_num = chapter.get("chapter")
    ch_title = chapter.get("title", "")
    pages = chapter.get("pages")
    genre = book.get("genre", "business")
    slug = book.get("slug")

    # 本のファイルパス解決（yagibrary 相対パスの可能性を考慮）
    raw_file_path = chapter.get("file_path") or book.get("file_path")
    if not raw_file_path:
        raise ValueError(f"書籍 {book_id} に file_path が指定されていません。")

    if not os.path.isabs(raw_file_path):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        candidates = [
            raw_file_path,
            os.path.normpath(os.path.join(base_dir, "../yagibrary", raw_file_path)),
            os.path.normpath(os.path.join(base_dir, raw_file_path)),
            os.path.normpath(os.path.join(os.path.dirname(queue_path), "..", raw_file_path)),
        ]
        resolved_file = None
        for c in candidates:
            if os.path.exists(c):
                resolved_file = c
                break
        if not resolved_file:
            raise FileNotFoundError(f"書籍ファイルが見つかりません: {raw_file_path} (探索候補: {candidates})")
    else:
        resolved_file = raw_file_path

    # カスタムファイル名の決定（例: rich-dads-cashflow-quadrant-ch3.md）
    custom_filename = None
    if slug and ch_num:
        custom_filename = f"{slug}-ch{ch_num}.md"

    print(f"\n📖 次の対象タスク:")
    print(f"   書籍: {book.get('title')} (ID: {book_id})")
    print(f"   章: {ch_title} (第{ch_num}章)")
    print(f"   ファイル: {resolved_file}")
    if pages:
        print(f"   ページ範囲: {pages}")
    if custom_filename:
        print(f"   保存ファイル名: {custom_filename}")

    if dry_run:
        print("\n🔎 [DRY RUN] 記事生成はスキップしました。キューとファイルパスの整合性は正常です。")
        return []

    # 記事執筆を実行！
    out_files = run_file_pipeline(
        file_path=resolved_file,
        pages=pages,
        chapter=ch_title,
        genre=genre,
        custom_filename=custom_filename,
        output_dir=output_dir,
        extra_tags=book.get("tags"),
    )

    if out_files:
        created_file = out_files[0]
        created_filename = os.path.basename(created_file)
        today_str = datetime.now().strftime("%Y-%m-%d")

        # キュー更新
        chapter["status"] = "published"
        chapter["post_file"] = created_filename
        chapter["published_at"] = today_str
        queue_data["active_book_id"] = book_id

        save_book_queue(queue_data, queue_path)
        print(f"\n✅ キューを正常に更新しました: {book_id} / 第{ch_num}章 -> published ({created_filename})")

    return out_files
