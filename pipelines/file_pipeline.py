"""pipelines/file_pipeline.py
ローカルファイル（PDF/Markdown）から自律的に解説記事を執筆・保存するパイプライン
"""

import os
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

from config import DEFAULT_YAGIBRARY_POSTS_DIR
from core.doc_reader import load_and_process_local_file
from core.post_formatter import (
    load_existing_posts_index,
    find_relevant_past_posts,
    format_doc_post_for_yagibrary,
)
from generators.doc_generator import generate_refined_doc_blog_post
from .pipeline_utils import resolve_output_filepath


def run_file_pipeline(
    file_path: str,
    pages: Optional[str] = None,
    chapter: Optional[str] = None,
    genre: Optional[str] = "auto",
    offset: Optional[int] = None,
    output_dir: Optional[str] = None,
    custom_filename: Optional[str] = None,
    extra_tags: Optional[List[str]] = None,
) -> List[str]:
    """ローカルファイル（PDF/Markdown）から自律的に解説記事を執筆・保存するパイプライン"""
    print("\n" + "=" * 65)
    print(" 📖 Local Document × TypeSafe Jev × Gemini ドキュメントブロガー 起動")
    print(f" 📂 指定ファイル: {file_path}")
    if pages:
        print(f" 📑 指定ページ: {pages}")
    if chapter:
        print(f" 🎯 指定章/テーマ: {chapter}")
    if offset is not None:
        print(f" 📏 指定オフセット: +{offset} ページ")
    if genre and genre != "auto":
        print(f" 📚 指定ジャンル: {genre}")
    if custom_filename:
        print(f" 🏷️ 指定保存ファイル名: {custom_filename}")
    if extra_tags:
        print(f" 🏷️ 固定付与タグ: {extra_tags}")
    print("=" * 65)

    if output_dir is None:
        if os.path.exists(DEFAULT_YAGIBRARY_POSTS_DIR):
            target_dir = DEFAULT_YAGIBRARY_POSTS_DIR
        else:
            target_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "generated_posts")
    else:
        target_dir = output_dir

    os.makedirs(target_dir, exist_ok=True)

    # 1. ファイル読込 & メタデータ抽出（Jev によるジャンル自動分類を含む）
    doc_info = load_and_process_local_file(
        file_path,
        pages_str=pages,
        chapter_hint=chapter,
        genre=genre,
        offset=offset,
    )

    # 過去記事インデックスから関連する記事を自動検索
    posts_index = load_existing_posts_index(target_dir)
    doc_tags = doc_info.get("categories", []) + [doc_info.get("subfield", "")]
    relevant_posts = find_relevant_past_posts(
        current_title=doc_info.get("title", ""),
        current_tags=doc_tags,
        current_text=doc_info.get("summary", ""),
        posts_index=posts_index,
        max_matches=3,
    )
    if relevant_posts:
        print(f"  🔗 関連する過去記事を {len(relevant_posts)} 件検出: {[p['title'][:30] for p in relevant_posts]}")

    # 2. 自律執筆 ＆ Jev推敲ループ
    raw_markdown, quality, history = generate_refined_doc_blog_post(
        doc_info,
        max_revisions=2,
        relevant_posts=relevant_posts,
    )

    # 3. Astro 向け整形
    final_post = format_doc_post_for_yagibrary(
        raw_markdown,
        doc_info,
        quality,
        history=history,
        relevant_posts=relevant_posts,
        figures=doc_info.get("figures"),
        extra_tags=extra_tags,
    )

    # 4. ファイル名生成 & 保存（日付プレフィックスは付与せず、意味のある英字スラッグを使用）
    base_name = os.path.splitext(doc_info["file_name"])[0]
    raw_slug = doc_info.get("slug") or ""
    base_slug = raw_slug or base_name or (f"{doc_info.get('genre', 'doc')}-note")

    out_file_path = resolve_output_filepath(
        base_slug=base_slug,
        target_dir=target_dir,
        pages=pages,
        chapter=chapter,
        custom_filename=custom_filename,
    )

    with open(out_file_path, "w", encoding="utf-8") as f:
        f.write(final_post)

    print("\n" + "=" * 65)
    print(f" 🎉 ドキュメント解説記事の生成・保存が完了しました！")
    print(f"    保存先: {out_file_path}")
    print("=" * 65)
    return [out_file_path]
