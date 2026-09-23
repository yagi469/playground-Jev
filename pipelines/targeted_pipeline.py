"""
pipelines/targeted_pipeline.py
==============================
特定の arXiv ID をピンポイントで取得・執筆するパイプライン
"""

import os
from datetime import datetime
from typing import List, Optional

from config import DEFAULT_YAGIBRARY_POSTS_DIR
from core.arxiv_client import fetch_arxiv_papers_by_ids, fetch_arxiv_paper_content
from core.figure_extractor import fetch_arxiv_paper_figures
from core.post_formatter import (
    load_existing_posts_index,
    find_relevant_past_posts,
    format_post_for_yagibrary,
)
from generators.paper_generator import (
    screen_and_rank_papers_with_jev,
    generate_refined_blog_post,
)


def run_targeted_pipeline(arxiv_ids: List[str], output_dir: Optional[str] = None) -> List[str]:
    """特定の arXiv ID を指定してピンポイントでブログ記事を執筆・保存するモード"""
    print("\n" + "=" * 65)
    print(" 🎯 arXiv × TypeSafe Jev × Gemini 特定論文ブロガー 起動")
    print(f" 📑 指定論文: {', '.join(arxiv_ids)}")
    print("=" * 65)

    if output_dir is None:
        if os.path.exists(DEFAULT_YAGIBRARY_POSTS_DIR):
            target_dir = DEFAULT_YAGIBRARY_POSTS_DIR
        else:
            target_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "generated_posts")
    else:
        target_dir = output_dir

    papers = fetch_arxiv_papers_by_ids(arxiv_ids)
    if not papers:
        print("❌ 指定された論文を取得できませんでした。")
        return []

    ranked_papers = screen_and_rank_papers_with_jev(papers)
    posts_index = load_existing_posts_index(target_dir)

    os.makedirs(target_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    generated_files = []

    for i, paper in enumerate(ranked_papers):
        batch_idx = i + 1
        print("\n" + "-" * 65)
        print(f" 🖋️ [記事執筆・整形 {batch_idx}/{len(ranked_papers)}] 対象論文: {paper['arxiv_id']}")
        print(f"    タイトル: {paper['title']}")
        print("-" * 65)

        paper_tags = paper.get("categories", []) + [paper.get("jev_metrics", {}).get("subfield", "")]
        relevant_posts = find_relevant_past_posts(
            current_title=paper.get("title", ""),
            current_tags=paper_tags,
            current_text=paper.get("summary", ""),
            posts_index=posts_index,
            max_matches=3,
        )
        if relevant_posts:
            print(f"  🔗 関連する過去記事を {len(relevant_posts)} 件検出: {[p['title'][:30] for p in relevant_posts]}")

        paper["full_text_content"] = fetch_arxiv_paper_content(paper["arxiv_id"])
        paper["figures"] = fetch_arxiv_paper_figures(paper)

        raw_markdown, quality, history = generate_refined_blog_post(
            paper=paper,
            rank=batch_idx,
            max_revisions=2,
            relevant_posts=relevant_posts,
        )

        time_offset = (len(ranked_papers) - batch_idx) * 60
        final_post = format_post_for_yagibrary(
            raw_markdown,
            paper,
            quality,
            rank=batch_idx,
            time_offset_seconds=time_offset,
            history=history,
            relevant_posts=relevant_posts,
            figures=paper.get("figures"),
        )

        clean_id = paper['arxiv_id'].replace('/', '_').replace('.', '-')
        filename = f"{today_str}-arxiv-{clean_id}.md"
        file_path = os.path.join(target_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_post)

        generated_files.append(file_path)
        print(f" ✅ [指定論文] 記事保存完了: {file_path}")

    print("\n" + "=" * 65)
    print(f" 🎉 全 {len(generated_files)} 件のブログ記事の生成が完了しました！")
    for idx, fp in enumerate(generated_files):
        print(f"   [{idx+1}] {fp}")
    print("=" * 65)
    return generated_files
